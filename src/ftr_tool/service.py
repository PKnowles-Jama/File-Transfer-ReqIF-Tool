from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .client import JamaClient, Project
from .configuration import (
    FieldConfiguration,
    ItemTypeConfiguration,
    JamaConfiguration,
    PicklistConfiguration,
    PicklistOptionConfiguration,
    RelationshipRuleConfiguration,
    UserConfiguration,
)
from .errors import JamaError

StatusCallback = Callable[[str], None]


@dataclass(frozen=True)
class Change:
    kind: str
    key: str
    message: str
    payload: dict[str, Any]


def _field_value(row: dict[str, Any], key: str, default: Any = None) -> Any:
    nested = row.get("fields", {})
    nested_value = nested.get(key, default) if isinstance(nested, dict) else default
    return row.get(key, nested_value)


class ConfigurationService:
    def __init__(self, client: JamaClient, logger: logging.Logger | None = None):
        self.client = client
        self.logger = logger or logging.getLogger("ftr_tool")

    def export_project(self, project: Project, status: StatusCallback = lambda _: None) -> JamaConfiguration:
        status("Retrieving item types and fields…")
        item_types = self._get_item_types(project.id)
        status("Retrieving picklists and options…")
        picklists = self._get_picklists(project.id, item_types)
        status("Retrieving relationship rules…")
        rules = self._get_relationship_rules(project.id, item_types)
        if not rules:
            status("The specified project does not implement a defined Traceability Information Model.")
        status("Retrieving project users…")
        users = self._get_users(project.id)
        status(f"Loaded {len(item_types)} item types, {len(picklists)} picklists, {len(rules)} relationship rules, and {len(users)} users.")
        return JamaConfiguration(
            source={"projectId": project.id, "projectKey": project.key, "projectName": project.name},
            itemTypes=item_types,
            picklists=picklists,
            relationshipRules=rules,
            users=users,
        )

    def compare(self, desired: JamaConfiguration, current: JamaConfiguration) -> list[Change]:
        changes: list[Change] = []
        current_picklists = {row.name.casefold(): row for row in current.picklists}
        for picklist in desired.picklists:
            existing = current_picklists.get(picklist.name.casefold())
            if not existing:
                changes.append(Change("picklist", f"picklist:{picklist.name}", f"Create picklist ‘{picklist.name}’", asdict(picklist)))
                continue
            existing_options = {row.name.casefold() for row in existing.options}
            for option in picklist.options:
                if option.name.casefold() not in existing_options:
                    payload = asdict(option) | {"picklistName": picklist.name, "targetPicklistId": existing.id}
                    changes.append(Change("picklist_option", f"option:{picklist.name}:{option.name}", f"Add option ‘{option.name}’ to ‘{picklist.name}’", payload))

        current_types = {row.name.casefold(): row for row in current.itemTypes}
        for item_type in desired.itemTypes:
            existing = current_types.get(item_type.name.casefold())
            if not existing:
                changes.append(Change("item_type", f"type:{item_type.name}", f"Create item type ‘{item_type.name}’ with its fields", asdict(item_type)))
                continue
            existing_fields = {row.name.casefold(): row for row in existing.fields}
            for config_field in item_type.fields:
                candidate = existing_fields.get(config_field.name.casefold())
                if candidate and self._same_field(candidate, config_field):
                    continue
                field_payload = asdict(config_field) | {"itemTypeName": item_type.name, "targetItemTypeId": existing.id}
                if candidate:
                    field_payload["name"] = self._available_field_name(config_field.name, existing_fields)
                changes.append(Change("field", f"field:{item_type.name}:{config_field.name}", f"Add field ‘{field_payload['name']}’ to ‘{item_type.name}’", field_payload))

        current_rules = {self._rule_key(row) for row in current.relationshipRules}
        for rule in desired.relationshipRules:
            if self._rule_key(rule) not in current_rules:
                label = f"{rule.fromItemTypeName or rule.fromItemTypeId} → {rule.toItemTypeName or rule.toItemTypeId} ({rule.relationshipTypeName or rule.relationshipTypeId})"
                changes.append(Change("relationship_notice", f"rule:{self._rule_key(rule)}", f"Manual TIM configuration required: {label}", asdict(rule)))
        current_users = {(row.username or row.email).casefold() for row in current.users}
        for user in desired.users:
            identity = (user.username or user.email).casefold()
            if identity and identity not in current_users:
                changes.append(Change("user", f"user:{identity}", f"Create user ‘{user.username or user.email}’", asdict(user)))
        return changes

    def apply(self, project_id: int, changes: list[Change], status: StatusCallback = lambda _: None) -> list[str]:
        outcomes: list[str] = []
        created_picklists: dict[str, int] = {}
        created_types: dict[str, int] = {}
        ordered = {"picklist": 0, "picklist_option": 1, "item_type": 2, "field": 3, "user": 4, "relationship_notice": 5}
        for change in sorted(changes, key=lambda row: ordered[row.kind]):
            status(change.message)
            try:
                if change.kind == "picklist":
                    result = self._create_picklist(project_id, change.payload)
                    created_picklists[change.payload["name"].casefold()] = int(result["id"])
                elif change.kind == "picklist_option":
                    target = change.payload.get("targetPicklistId") or created_picklists.get(change.payload["picklistName"].casefold())
                    self._create_picklist_option(int(target), change.payload)
                elif change.kind == "item_type":
                    result = self._create_item_type(project_id, change.payload, created_picklists)
                    created_types[change.payload["name"].casefold()] = int(result["id"])
                elif change.kind == "field":
                    target = change.payload.get("targetItemTypeId") or created_types.get(change.payload["itemTypeName"].casefold())
                    self._create_field(int(target), change.payload, created_picklists)
                elif change.kind == "user":
                    self._create_user(change.payload)
                else:
                    outcomes.append("NOTICE: " + change.message)
                    continue
                outcomes.append("SUCCESS: " + change.message)
                self.logger.info("Applied: %s", change.message)
            except Exception as exc:
                message = f"FAILED: {change.message}: {exc}"
                outcomes.append(message)
                self.logger.exception(message)
        return outcomes

    def _get_item_types(self, project_id: int) -> list[ItemTypeConfiguration]:
        used_type_ids = {
            int(row["itemType"])
            for row in self.client.paged("abstractitems", {"project": project_id})
            if row.get("itemType") is not None
        }
        rows = [self.client.request(f"itemtypes/{item_type_id}") for item_type_id in sorted(used_type_ids)]
        result: list[ItemTypeConfiguration] = []
        for row in rows:
            fields = row.get("fields", [])
            if isinstance(fields, dict):
                fields = fields.get("fields", [])
            result.append(ItemTypeConfiguration(
                id=int(row["id"]),
                name=str(row.get("display") or row.get("name") or row.get("typeKey") or row["id"]),
                fields=[self._parse_field(value) for value in fields if isinstance(value, dict) and value.get("name")],
            ))
        return result

    def _get_picklists(self, project_id: int, item_types: list[ItemTypeConfiguration]) -> list[PicklistConfiguration]:
        used_picklist_ids = {field.picklist for item_type in item_types for field in item_type.fields if field.picklist is not None}
        rows = self.client.paged("picklists", {"project": project_id})
        if used_picklist_ids:
            rows = [row for row in rows if int(row["id"]) in used_picklist_ids]
        else:
            rows = []
        result: list[PicklistConfiguration] = []
        for row in rows:
            picklist_id = int(row["id"])
            options = self.client.paged(f"picklists/{picklist_id}/options")
            result.append(PicklistConfiguration(
                id=picklist_id,
                name=str(_field_value(row, "name", "")),
                options=[PicklistOptionConfiguration(int(value["id"]), str(_field_value(value, "name", "")), bool(_field_value(value, "default", False))) for value in options],
            ))
        return result

    def _get_relationship_rules(self, project_id: int, item_types: list[ItemTypeConfiguration]) -> list[RelationshipRuleConfiguration]:
        names = {row.id: row.name for row in item_types}
        try:
            ruleset = self.client.request_first([
                f"projects/{project_id}/relationshipruleset",
                f"relationshiprulesets?project={project_id}",
            ])
            if isinstance(ruleset, list):
                ruleset = ruleset[0] if ruleset else None
            if not ruleset:
                return []
            ruleset_id = ruleset.get("id")
            rules = ruleset.get("relationships") or self.client.paged(f"relationshiprulesets/{ruleset_id}/relationships")
            return [RelationshipRuleConfiguration(
                id=value.get("id"),
                fromItemTypeId=value.get("fromItemTypeId"),
                toItemTypeId=value.get("toItemTypeId"),
                forCoverage=bool(value.get("forCoverage", False)),
                relationshipTypeId=value.get("relationshipTypeId"),
                fromItemTypeName=names.get(value.get("fromItemTypeId")),
                toItemTypeName=names.get(value.get("toItemTypeId")),
                relationshipTypeName=value.get("relationshipTypeName"),
            ) for value in rules]
        except JamaError as exc:
            self.logger.warning("Relationship rule set unavailable: %s", exc)
            return []

    @staticmethod
    def _parse_field(value: dict[str, Any]) -> FieldConfiguration:
        field_type = str(value.get("fieldType", "STRING"))
        if field_type.upper() == "USER":
            field_type = "STRING"
        picklist = value.get("picklist", value.get("pickList"))
        return FieldConfiguration(
            name=str(value["name"]), label=str(value.get("label", value["name"])), fieldType=field_type,
            readOnly=bool(value.get("readOnly", False)), readOnlyAllowApiOverwrite=bool(value.get("readOnlyAllowApiOverwrite", False)),
            required=bool(value.get("required", False)), triggerSuspect=bool(value.get("triggerSuspect", False)),
            synchronize=bool(value.get("synchronize", False)), picklist=picklist.get("id") if isinstance(picklist, dict) else picklist,
            picklistName=picklist.get("name") if isinstance(picklist, dict) else value.get("picklistName"),
            textType=value.get("textType"), infotip=value.get("infotip"),
        )

    @staticmethod
    def _same_field(left: FieldConfiguration, right: FieldConfiguration) -> bool:
        return (left.fieldType.upper(), left.picklistName, left.textType) == (right.fieldType.upper(), right.picklistName, right.textType)

    @staticmethod
    def _available_field_name(name: str, existing: dict[str, FieldConfiguration]) -> str:
        candidate = name + "1"
        suffix = 1
        while candidate.casefold() in existing:
            suffix += 1
            candidate = f"{name}{suffix}"
        return candidate

    @staticmethod
    def _rule_key(rule: RelationshipRuleConfiguration) -> tuple[Any, ...]:
        return (rule.fromItemTypeName or rule.fromItemTypeId, rule.toItemTypeName or rule.toItemTypeId, rule.relationshipTypeName or rule.relationshipTypeId, rule.forCoverage)

    def _create_picklist(self, project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.request("picklists", method="POST", data={"project": project_id, "name": payload["name"]})

    def _create_picklist_option(self, picklist_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.request(f"picklists/{picklist_id}/options", method="POST", data={"name": payload["name"], "default": payload.get("default", False)})

    def _create_item_type(self, project_id: int, payload: dict[str, Any], picklists: dict[str, int]) -> dict[str, Any]:
        fields = [self._field_payload(value, picklists) for value in payload.get("fields", [])]
        return self.client.request("itemtypes", method="POST", data={"project": project_id, "name": payload["name"], "fields": fields})

    def _create_field(self, item_type_id: int, payload: dict[str, Any], picklists: dict[str, int]) -> dict[str, Any]:
        return self.client.request(f"itemtypes/{item_type_id}/fields", method="POST", data=self._field_payload(payload, picklists))

    def _get_users(self, project_id: int) -> list[UserConfiguration]:
        try:
            rows = self.client.paged("users", {"project": project_id})
        except JamaError as exc:
            self.logger.warning("Project users unavailable: %s", exc)
            return []
        return [UserConfiguration(
            id=int(row["id"]) if row.get("id") is not None else None,
            username=str(row.get("username", "")), email=str(row.get("email", "")),
            firstName=str(row.get("firstName", "")), lastName=str(row.get("lastName", "")),
            active=bool(row.get("active", True)),
        ) for row in rows if row.get("username") or row.get("email")]

    def _create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"username", "email", "firstName", "lastName", "active"}
        return self.client.request("users", method="POST", data={key: value for key, value in payload.items() if key in allowed})

    @staticmethod
    def _field_payload(payload: dict[str, Any], picklists: dict[str, int]) -> dict[str, Any]:
        allowed = {"name", "label", "fieldType", "readOnly", "readOnlyAllowApiOverwrite", "required", "triggerSuspect", "synchronize", "textType", "infotip"}
        result = {key: value for key, value in payload.items() if key in allowed and value is not None}
        picklist_name = payload.get("picklistName")
        if picklist_name and picklist_name.casefold() in picklists:
            result["picklist"] = picklists[picklist_name.casefold()]
        elif payload.get("picklist") is not None:
            result["picklist"] = payload["picklist"]
        return result
