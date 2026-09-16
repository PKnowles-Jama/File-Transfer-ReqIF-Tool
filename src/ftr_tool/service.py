from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

from .client import Project
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

SUPPORTED_REQUEST_FIELD_TYPES = {
    "MULTI_LOOKUP",
    "STRING",
    "RELEASE",
    "BOOLEAN",
    "LOOKUP",
    "USER",
    "FLOAT",
    "TEXT",
    "DATE",
    "URL_STRING",
    "INTEGER",
}

AUTO_CREATED_NEW_ITEM_TYPE_FIELD_NAMES = {
    "assigned",
    "description",
    "document_key",
    "documentkey",
    "global_id",
    "globalid",
    "name",
}

AUTO_CREATED_NEW_ITEM_TYPE_FIELD_LABELS = {
    "assigned",
    "description",
    "document key",
    "global id",
    "name",
    "project id",
}


@dataclass(frozen=True)
class Change:
    kind: str
    key: str
    message: str
    payload: dict[str, Any]


class JamaClientLike(Protocol):
    def request(self, path: str, method: str = "GET", query: dict[str, Any] | None = None, data: Any | None = None) -> Any: ...

    def paged(self, path: str, query: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def request_first(self, paths: list[str], **kwargs: Any) -> Any: ...


def _field_value(row: dict[str, Any], key: str, default: Any = None) -> Any:
    nested = row.get("fields", {})
    nested_value = nested.get(key, default) if isinstance(nested, dict) else default
    return row.get(key, nested_value)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ConfigurationService:
    def __init__(self, client: JamaClientLike | None, logger: logging.Logger | None = None):
        self.client = client
        self.logger = logger or logging.getLogger("ftr_tool")
        self._instance_item_types_cache: dict[str, ItemTypeConfiguration] | None = None
        self._instance_picklists_cache: dict[str, PicklistConfiguration] | None = None

    def invalidate_instance_metadata_cache(self) -> None:
        self._instance_item_types_cache = None
        self._instance_picklists_cache = None

    def export_project(self, project: Project, status: StatusCallback = lambda _: None) -> JamaConfiguration:
        status("Retrieving item types and fields…")
        item_types = self._get_item_types(project.id)
        status("Retrieving picklists and options…")
        picklists = self._get_picklists(project.id, item_types)
        picklist_names = {row.id: row.name for row in picklists if row.id is not None}
        for item_type in item_types:
            for field in item_type.fields:
                picklist_id = _int_or_none(field.picklist)
                if not field.picklistName and picklist_id is not None and picklist_id in picklist_names:
                    field.picklistName = picklist_names[picklist_id]
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
        instance_types = self._instance_item_types_by_name()
        instance_picklists = self._instance_picklists_by_name()
        desired_picklist_names = {row.id: row.name for row in desired.picklists if row.id is not None}
        current_picklist_names = {row.id: row.name for row in current.picklists if row.id is not None}
        instance_picklist_names = {row.id: row.name for row in instance_picklists.values() if row.id is not None}
        current_picklists = {row.name.casefold(): row for row in current.picklists}
        target_picklists = dict(instance_picklists)
        target_picklists.update(current_picklists)
        for picklist in desired.picklists:
            existing = current_picklists.get(picklist.name.casefold())
            if not existing:
                instance_match = instance_picklists.get(picklist.name.casefold())
                if not instance_match:
                    changes.append(Change("picklist", f"picklist:{picklist.name}", f"Create picklist ‘{picklist.name}’", asdict(picklist)))
                    for option in picklist.options:
                        payload = asdict(option) | {"picklistName": picklist.name}
                        changes.append(Change(
                            "picklist_option",
                            f"option:{picklist.name}:{option.name}",
                            f"Add option ‘{option.name}’ to ‘{picklist.name}’",
                            payload,
                        ))
                    continue
                changes.append(Change(
                    "instance_picklist_notice",
                    f"instance-picklist:{picklist.name}",
                    f"The existing Jama Connect picklist '{picklist.name}' is already available in the instance.",
                    {"picklistName": picklist.name, "instancePicklistId": instance_match.id},
                ))
                existing_instance_options = {row.name.casefold() for row in instance_match.options}
                for option in picklist.options:
                    if option.name.casefold() not in existing_instance_options:
                        payload = asdict(option) | {"picklistName": picklist.name, "targetPicklistId": instance_match.id}
                        changes.append(Change(
                            "instance_picklist_option",
                            f"instance-option:{picklist.name}:{option.name}",
                            f"Add option '{option.name}' to existing Jama Connect picklist '{picklist.name}'",
                            payload,
                        ))
                continue
            existing_options = {row.name.casefold() for row in existing.options}
            for option in picklist.options:
                if option.name.casefold() not in existing_options:
                    payload = asdict(option) | {"picklistName": picklist.name, "targetPicklistId": existing.id}
                    changes.append(Change("picklist_option", f"option:{picklist.name}:{option.name}", f"Add option ‘{option.name}’ to ‘{picklist.name}’", payload))

        current_types = {row.name.casefold(): row for row in current.itemTypes}
        current_types_by_id = {row.id: row for row in current.itemTypes if row.id is not None}
        instance_types_by_id = {row.id: row for row in instance_types.values() if row.id is not None}
        for item_type in desired.itemTypes:
            existing = current_types.get(item_type.name.casefold())
            if not existing:
                associated_target, associated_in_instance = self._resolve_associated_item_type(
                    item_type,
                    current_types,
                    current_types_by_id,
                    instance_types,
                    instance_types_by_id,
                )
                if associated_target is not None:
                    if associated_in_instance:
                        notice_payload = {
                            "itemTypeName": associated_target.name,
                            "instanceItemTypeId": associated_target.id,
                            "associatedFromItemTypeName": item_type.name,
                        }
                        changes.append(Change(
                            "instance_item_type_notice",
                            f"instance-type-associated:{item_type.name}:{associated_target.name}",
                            f"The associated item type {associated_target.name} for {item_type.name} is already available in the Jama Connect instance.",
                            notice_payload,
                        ))
                    self._append_field_changes_for_mapped_item_type(
                        changes=changes,
                        source_item_type=item_type,
                        target_item_type=associated_target,
                        target_picklist_names=instance_picklist_names if associated_in_instance else current_picklist_names,
                        desired_picklist_names=desired_picklist_names,
                        target_picklists=target_picklists,
                        kind="instance_field" if associated_in_instance else "field",
                    )
                    continue
                instance_match = instance_types.get(item_type.name.casefold())
                if not instance_match:
                    item_type_payload = {
                        "id": item_type.id,
                        "name": item_type.name,
                        "typeKey": item_type.typeKey,
                        "display": item_type.display,
                        "displayPlural": item_type.displayPlural,
                        "fields": [],
                    }
                    changes.append(Change("item_type", f"type:{item_type.name}", f"Create item type ‘{item_type.name}’ with its fields", item_type_payload))
                    self._append_field_changes_for_new_item_type(
                        changes=changes,
                        item_type=item_type,
                        desired_picklist_names=desired_picklist_names,
                        target_picklists=target_picklists,
                    )
                    continue
                notice_payload = {"itemTypeName": item_type.name, "instanceItemTypeId": instance_match.id}
                changes.append(Change(
                    "instance_item_type_notice",
                    f"instance-type:{item_type.name}",
                    f"The existing Jama Connect item type '{item_type.name}' is already available in the instance.",
                    notice_payload,
                ))
                for config_field in item_type.fields:
                    equivalent = self._find_equivalent_field(
                        instance_match.fields,
                        config_field,
                        instance_picklist_names,
                        desired_picklist_names,
                    )
                    if equivalent and self._same_field(equivalent, config_field, instance_picklist_names, desired_picklist_names):
                        continue
                    field_payload = self._field_change_payload(config_field, desired_picklist_names, target_picklists) | {
                        "itemTypeName": item_type.name,
                        "targetItemTypeId": instance_match.id,
                    }
                    conflict = self._find_conflicting_field(instance_match.fields, field_payload["name"])
                    if conflict:
                        if self._same_field(conflict, config_field, instance_picklist_names, desired_picklist_names):
                            continue
                        conflict_payload = dict(field_payload)
                        conflict_payload["existingFieldName"] = conflict.name
                        conflict_payload["existingFieldLabel"] = conflict.label
                        changes.append(Change(
                            "field_notice",
                            f"field-conflict:{item_type.name}:{config_field.name}",
                            f"Manual field configuration required for ‘{field_payload['label']}’ on ‘{item_type.name}’: field name '{field_payload['name']}' already exists.",
                            conflict_payload,
                        ))
                        continue
                    if self._request_field_type(config_field.fieldType) is None:
                        changes.append(Change(
                            "field_notice",
                            f"field-notice:{item_type.name}:{config_field.name}",
                            f"Manual field configuration required for ‘{field_payload['label']}’ on ‘{item_type.name}’",
                            field_payload,
                        ))
                        continue
                    changes.append(Change(
                        "instance_field",
                        f"instance-field:{item_type.name}:{config_field.name}",
                        f"Add field '{field_payload['name']}' with '{config_field.fieldType}' to existing Jama Connect item type '{item_type.name}'",
                        field_payload,
                    ))
                continue
            existing_fields = {row.name.casefold(): row for row in existing.fields}
            for config_field in item_type.fields:
                candidate = existing_fields.get(config_field.name.casefold())
                equivalent = candidate or self._find_equivalent_field(existing.fields, config_field, current_picklist_names, desired_picklist_names)
                if equivalent and self._same_field(equivalent, config_field, current_picklist_names, desired_picklist_names):
                    continue
                field_payload = self._field_change_payload(config_field, desired_picklist_names, target_picklists) | {
                    "itemTypeName": item_type.name,
                    "targetItemTypeId": existing.id,
                }
                conflict = self._find_conflicting_field(existing.fields, field_payload["name"])
                if conflict:
                    if self._same_field(conflict, config_field, current_picklist_names, desired_picklist_names):
                        continue
                    conflict_payload = dict(field_payload)
                    conflict_payload["existingFieldName"] = conflict.name
                    conflict_payload["existingFieldLabel"] = conflict.label
                    changes.append(Change(
                        "field_notice",
                        f"field-conflict:{item_type.name}:{config_field.name}",
                        f"Manual field configuration required for ‘{field_payload['label']}’ on ‘{item_type.name}’: field name '{field_payload['name']}' already exists.",
                        conflict_payload,
                    ))
                    continue
                if self._request_field_type(config_field.fieldType) is None:
                    changes.append(Change(
                        "field_notice",
                        f"field-notice:{item_type.name}:{config_field.name}",
                        f"Manual field configuration required for ‘{field_payload['label']}’ on ‘{item_type.name}’",
                        field_payload,
                    ))
                    continue
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

    def association_candidates(
        self,
        desired: JamaConfiguration,
        current: JamaConfiguration,
    ) -> dict[str, list[ItemTypeConfiguration]]:
        current_types = {row.name.casefold(): row for row in current.itemTypes}
        instance_types = self._instance_item_types_by_name()
        available_targets: dict[str, ItemTypeConfiguration] = {row.name.casefold(): row for row in current.itemTypes}
        for key, value in instance_types.items():
            if key not in available_targets:
                available_targets[key] = value
        result: dict[str, list[ItemTypeConfiguration]] = {}
        for item_type in desired.itemTypes:
            if item_type.name.casefold() in current_types:
                continue
            if item_type.name.casefold() in instance_types:
                continue
            if _int_or_none(item_type.associatedItemTypeId) is not None or (item_type.associatedItemTypeName or "").strip():
                continue
            options = [
                self._copy_item_type(row)
                for key, row in available_targets.items()
                if key != item_type.name.casefold()
            ]
            if options:
                options.sort(key=lambda row: row.name.casefold())
                result[item_type.name] = options
        return result

    def copy_configuration(self, value: JamaConfiguration) -> JamaConfiguration:
        return JamaConfiguration(
            source=dict(value.source),
            itemTypes=[self._copy_item_type(row) for row in value.itemTypes],
            picklists=[self._copy_picklist(row) for row in value.picklists],
            relationshipRules=[RelationshipRuleConfiguration(**asdict(row)) for row in value.relationshipRules],
            users=[self._copy_user(row) for row in value.users],
            format=value.format,
            version=value.version,
            exportedAt=value.exportedAt,
        )

    @staticmethod
    def apply_item_type_association(
        item_type: ItemTypeConfiguration,
        associated_item_type: ItemTypeConfiguration | None,
    ) -> None:
        if associated_item_type is None:
            item_type.associatedItemTypeId = None
            item_type.associatedItemTypeName = None
            return
        item_type.associatedItemTypeId = associated_item_type.id
        item_type.associatedItemTypeName = associated_item_type.name

    def apply_picklist_mapping(
        self,
        configuration: JamaConfiguration,
        source_picklist_name: str,
        target_picklist_name: str | None,
    ) -> None:
        source = next((row for row in configuration.picklists if row.name.casefold() == source_picklist_name.casefold()), None)
        if source is None or not target_picklist_name:
            return
        old_name = source.name
        source.name = target_picklist_name
        for item_type in configuration.itemTypes:
            for field in item_type.fields:
                if field.picklistName and field.picklistName.casefold() == old_name.casefold():
                    field.picklistName = target_picklist_name
                    continue
                if source.id is not None and _int_or_none(field.picklist) == source.id:
                    field.picklistName = target_picklist_name

    def export_selected_configuration(self, desired: JamaConfiguration, selected: list[Change]) -> JamaConfiguration:
        filtered = [
            row
            for row in selected
            if row.kind not in ("relationship_notice", "instance_item_type_notice", "instance_picklist_notice")
        ]
        item_types_by_name = {row.name.casefold(): row for row in desired.itemTypes}
        picklists_by_name = {row.name.casefold(): row for row in desired.picklists}
        users_by_identity = {(row.username or row.email).casefold(): row for row in desired.users}

        item_types_out: dict[str, ItemTypeConfiguration] = {}
        picklists_out: dict[str, PicklistConfiguration] = {}
        users_out: dict[str, UserConfiguration] = {}

        for change in filtered:
            if change.kind == "picklist":
                key = str(change.payload["name"]).casefold()
                source = picklists_by_name.get(key)
                if source:
                    picklists_out[key] = PicklistConfiguration(id=source.id, name=source.name, options=[])
            elif change.kind in ("picklist_option", "instance_picklist_option"):
                picklist_name = str(change.payload.get("picklistName", ""))
                if not picklist_name:
                    continue
                picklist_key = picklist_name.casefold()
                source = picklists_by_name.get(picklist_key)
                if not source:
                    continue
                option_name = str(change.payload["name"]).casefold()
                option = next((row for row in source.options if row.name.casefold() == option_name), None)
                if not option:
                    continue
                if picklist_key not in picklists_out:
                    picklists_out[picklist_key] = PicklistConfiguration(id=source.id, name=source.name, options=[])
                if option.name.casefold() not in {row.name.casefold() for row in picklists_out[picklist_key].options}:
                    picklists_out[picklist_key].options.append(PicklistOptionConfiguration(id=option.id, name=option.name, default=option.default))
            elif change.kind == "item_type":
                key = str(change.payload["name"]).casefold()
                source = item_types_by_name.get(key)
                if source:
                    item_types_out[key] = ItemTypeConfiguration(
                        id=source.id,
                        name=source.name,
                        fields=[],
                        typeKey=source.typeKey,
                        display=source.display,
                        displayPlural=source.displayPlural,
                        associatedItemTypeName=source.associatedItemTypeName,
                        associatedItemTypeId=source.associatedItemTypeId,
                    )
            elif change.kind in ("field", "instance_field"):
                item_type_name = str(change.payload.get("itemTypeName", ""))
                if not item_type_name:
                    continue
                key = item_type_name.casefold()
                if key not in item_types_out:
                    source = item_types_by_name.get(key)
                    item_types_out[key] = ItemTypeConfiguration(
                        id=source.id if source else None,
                        name=item_type_name,
                        fields=[],
                        typeKey=source.typeKey if source else None,
                        display=source.display if source else item_type_name,
                        displayPlural=source.displayPlural if source else None,
                    )
                field = FieldConfiguration(
                    name=str(change.payload["name"]),
                    label=str(change.payload.get("label", change.payload["name"])),
                    fieldType=str(change.payload.get("fieldType", "STRING")),
                    readOnly=bool(change.payload.get("readOnly", False)),
                    readOnlyAllowApiOverwrite=bool(change.payload.get("readOnlyAllowApiOverwrite", False)),
                    required=bool(change.payload.get("required", False)),
                    triggerSuspect=bool(change.payload.get("triggerSuspect", False)),
                    synchronize=bool(change.payload.get("synchronize", False)),
                    picklist=change.payload.get("picklist"),
                    picklistName=change.payload.get("picklistName"),
                    textType=change.payload.get("textType"),
                    infotip=change.payload.get("infotip"),
                )
                existing_names = {row.name.casefold() for row in item_types_out[key].fields}
                if field.name.casefold() not in existing_names:
                    item_types_out[key].fields.append(field)
            elif change.kind == "user":
                identity = str(change.payload.get("username") or change.payload.get("email") or "").casefold()
                if not identity:
                    continue
                source = users_by_identity.get(identity)
                user_id = _int_or_none(change.payload.get("id"))
                users_out[identity] = self._copy_user(source) if source else UserConfiguration(
                    id=user_id,
                    username=str(change.payload.get("username", "")),
                    email=str(change.payload.get("email", "")),
                    firstName=str(change.payload.get("firstName", "")),
                    lastName=str(change.payload.get("lastName", "")),
                    active=bool(change.payload.get("active", True)),
                )

        return JamaConfiguration(
            source=desired.source | {"selectionMode": "yes-only-export"},
            itemTypes=list(item_types_out.values()),
            picklists=list(picklists_out.values()),
            relationshipRules=[],
            users=list(users_out.values()),
        )

    def apply(self, project_id: int, changes: list[Change], status: StatusCallback = lambda _: None) -> list[str]:
        outcomes: list[str] = []
        created_picklists: dict[str, int] = {}
        created_types: dict[str, int] = {}
        created_type_fields: dict[int, list[FieldConfiguration]] = {}
        existing_type_keys = self._existing_item_type_keys()
        ordered = {
            "picklist": 0,
            "picklist_option": 1,
            "instance_picklist_option": 2,
            "item_type": 3,
            "instance_field": 4,
            "field": 5,
            "user": 6,
            "field_notice": 7,
            "instance_picklist_notice": 8,
            "instance_item_type_notice": 9,
            "relationship_notice": 10,
        }
        for change in sorted(changes, key=lambda row: ordered[row.kind]):
            status(change.message)
            try:
                if change.kind == "picklist":
                    result = self._create_picklist(project_id, change.payload)
                    picklist_id = self._resolve_created_id(result) or self._find_picklist_id(project_id, change.payload["name"])
                    if picklist_id is None:
                        raise JamaError(f"Could not resolve created picklist ID for '{change.payload['name']}'.")
                    created_picklists[change.payload["name"].casefold()] = int(picklist_id)
                elif change.kind in ("picklist_option", "instance_picklist_option"):
                    target = change.payload.get("targetPicklistId") or created_picklists.get(change.payload["picklistName"].casefold())
                    if target is None:
                        raise JamaError(f"Could not resolve target picklist ID for '{change.payload['picklistName']}'.")
                    self._create_picklist_option(int(target), change.payload)
                elif change.kind == "item_type":
                    result, used_type_key = self._create_item_type_with_unique_key(
                        project_id,
                        change.payload,
                        created_picklists,
                        existing_type_keys,
                    )
                    item_type_id = self._resolve_created_id(result) or self._find_item_type_id(project_id, change.payload["name"])
                    if item_type_id is None:
                        raise JamaError(f"Could not resolve created item type ID for '{change.payload['name']}'.")
                    created_types[change.payload["name"].casefold()] = int(item_type_id)
                    created_type_fields[int(item_type_id)] = self._get_item_type_fields(int(item_type_id))
                    existing_type_keys.add(used_type_key.casefold())
                elif change.kind in ("field", "instance_field"):
                    target = change.payload.get("targetItemTypeId") or created_types.get(change.payload["itemTypeName"].casefold())
                    if target is None:
                        raise JamaError(f"Could not resolve target item type ID for '{change.payload['itemTypeName']}'.")
                    request_payload, reason = self._field_payload(change.payload, created_picklists)
                    if request_payload is None:
                        outcomes.append("NOTICE: " + (reason or change.message))
                        self.logger.warning("Skipped: %s (%s)", change.message, reason)
                        continue
                    created_fields = created_type_fields.get(int(target))
                    if created_fields is not None and self._find_conflicting_field(created_fields, request_payload["name"]):
                        outcomes.append("NOTICE: " + change.message)
                        self.logger.warning(
                            "Skipped: %s (the newly created item type already has a field named '%s').",
                            change.message,
                            request_payload["name"],
                        )
                        continue
                    self._create_field(int(target), request_payload, created_picklists, request_ready=True)
                    if created_fields is not None:
                        created_fields.append(FieldConfiguration(
                            name=str(request_payload["name"]),
                            label=str(request_payload.get("label") or request_payload["name"]),
                            fieldType=str(request_payload.get("fieldType", "STRING")),
                            readOnly=bool(request_payload.get("readOnly", False)),
                            readOnlyAllowApiOverwrite=bool(request_payload.get("readOnlyAllowApiOverwrite", False)),
                            required=bool(request_payload.get("required", False)),
                            triggerSuspect=bool(request_payload.get("triggerSuspect", False)),
                            synchronize=bool(request_payload.get("synchronize", False)),
                            picklist=_int_or_none(request_payload.get("pickList")),
                            picklistName=change.payload.get("picklistName"),
                            textType=request_payload.get("textType"),
                            infotip=request_payload.get("infotip"),
                        ))
                elif change.kind == "user":
                    self._create_user(change.payload)
                else:
                    outcomes.append("NOTICE: " + change.message)
                    continue
                outcomes.append("SUCCESS: " + change.message)
                self.logger.info("Applied: %s", change.message)
            except Exception as exc:
                if change.kind in ("field", "instance_field"):
                    notice = self._recoverable_field_apply_notice(change, exc)
                    if notice:
                        outcomes.append("NOTICE: " + notice)
                        self.logger.warning("Skipped: %s (%s)", change.message, exc)
                        continue
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
                typeKey=str(row.get("typeKey")) if row.get("typeKey") else None,
                display=str(row.get("display")) if row.get("display") else None,
                displayPlural=str(row.get("displayPlural")) if row.get("displayPlural") else None,
            ))
        return result

    def _instance_item_types_by_name(self) -> dict[str, ItemTypeConfiguration]:
        if self._instance_item_types_cache is not None:
            return self._instance_item_types_cache
        if self.client is None:
            self._instance_item_types_cache = {}
            return self._instance_item_types_cache
        try:
            rows = self.client.paged("itemtypes")
        except JamaError as exc:
            self.logger.warning("Instance item types unavailable: %s", exc)
            self._instance_item_types_cache = {}
            return self._instance_item_types_cache
        found: dict[str, ItemTypeConfiguration] = {}
        for row in rows:
            item_type_id = row.get("id")
            if item_type_id is None:
                continue
            try:
                detail = self.client.request(f"itemtypes/{int(item_type_id)}")
            except JamaError:
                continue
            raw_fields = detail.get("fields", [])
            if isinstance(raw_fields, dict):
                raw_fields = raw_fields.get("fields", [])
            name = str(detail.get("display") or detail.get("name") or detail.get("typeKey") or detail["id"])
            found[name.casefold()] = ItemTypeConfiguration(
                id=int(detail["id"]),
                name=name,
                fields=[self._parse_field(value) for value in raw_fields if isinstance(value, dict) and value.get("name")],
                typeKey=str(detail.get("typeKey")) if detail.get("typeKey") else None,
                display=str(detail.get("display")) if detail.get("display") else None,
                displayPlural=str(detail.get("displayPlural")) if detail.get("displayPlural") else None,
            )
        self._instance_item_types_cache = found
        return self._instance_item_types_cache

    def _instance_picklists_by_name(self) -> dict[str, PicklistConfiguration]:
        if self._instance_picklists_cache is not None:
            return self._instance_picklists_cache
        if self.client is None:
            self._instance_picklists_cache = {}
            return self._instance_picklists_cache
        try:
            rows = self.client.paged("picklists")
        except JamaError as exc:
            self.logger.warning("Instance picklists unavailable: %s", exc)
            self._instance_picklists_cache = {}
            return self._instance_picklists_cache
        found: dict[str, PicklistConfiguration] = {}
        for row in rows:
            picklist_id = row.get("id")
            if picklist_id is None:
                continue
            name = str(_field_value(row, "name", ""))
            if not name:
                continue
            try:
                options = self.client.paged(f"picklists/{int(picklist_id)}/options")
            except JamaError:
                options = []
            found[name.casefold()] = PicklistConfiguration(
                id=int(picklist_id),
                name=name,
                options=[
                    PicklistOptionConfiguration(
                        id=int(value["id"]) if value.get("id") is not None else None,
                        name=str(_field_value(value, "name", "")),
                        default=bool(_field_value(value, "default", False)),
                    )
                    for value in options
                    if _field_value(value, "name", "")
                ],
            )
        self._instance_picklists_cache = found
        return self._instance_picklists_cache

    @staticmethod
    def _copy_item_type(value: ItemTypeConfiguration) -> ItemTypeConfiguration:
        return ItemTypeConfiguration(
            id=value.id,
            name=value.name,
            fields=[ConfigurationService._copy_field(row) for row in value.fields],
            typeKey=value.typeKey,
            display=value.display,
            displayPlural=value.displayPlural,
            associatedItemTypeName=value.associatedItemTypeName,
            associatedItemTypeId=value.associatedItemTypeId,
        )

    @staticmethod
    def _copy_field(value: FieldConfiguration) -> FieldConfiguration:
        return FieldConfiguration(**asdict(value))

    @staticmethod
    def _copy_picklist(value: PicklistConfiguration) -> PicklistConfiguration:
        return PicklistConfiguration(
            id=value.id,
            name=value.name,
            options=[PicklistOptionConfiguration(id=row.id, name=row.name, default=row.default) for row in value.options],
        )

    @staticmethod
    def _copy_user(value: UserConfiguration) -> UserConfiguration:
        return UserConfiguration(**asdict(value))

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
    def _same_field(
        left: FieldConfiguration,
        right: FieldConfiguration,
        left_picklist_names: dict[int, str] | None = None,
        right_picklist_names: dict[int, str] | None = None,
    ) -> bool:
        left_type = ConfigurationService._comparable_field_type(left.fieldType)
        right_type = ConfigurationService._comparable_field_type(right.fieldType)
        if left_type != right_type:
            return False
        left_text = (left.textType or "").upper() or None
        right_text = (right.textType or "").upper() or None
        if left_text != right_text:
            return False
        left_picklist = ConfigurationService._picklist_ref_name(left, left_picklist_names)
        right_picklist = ConfigurationService._picklist_ref_name(right, right_picklist_names)
        return left_picklist == right_picklist

    @staticmethod
    def _picklist_ref_name(field: FieldConfiguration, names_by_id: dict[int, str] | None = None) -> str | None:
        if field.picklistName:
            return str(field.picklistName).casefold()
        picklist_id = _int_or_none(field.picklist)
        if names_by_id and picklist_id is not None and picklist_id in names_by_id:
            return names_by_id[picklist_id].casefold()
        return None

    @staticmethod
    def _normalized_field_name(name: str) -> str:
        return re.sub(r"\$\d+$", "", name).casefold()

    @staticmethod
    def _find_equivalent_field(
        existing: list[FieldConfiguration],
        desired: FieldConfiguration,
        existing_picklist_names: dict[int, str] | None = None,
        desired_picklist_names: dict[int, str] | None = None,
    ) -> FieldConfiguration | None:
        desired_name = ConfigurationService._normalized_field_name(desired.name)
        desired_request_name = ConfigurationService._request_field_name(desired.name).casefold()
        desired_label = ConfigurationService._normalized_label(desired.label or desired.name)
        for candidate in existing:
            candidate_matches = (
                ConfigurationService._normalized_field_name(candidate.name) == desired_name
                or ConfigurationService._request_field_name(candidate.name).casefold() == desired_request_name
                or ConfigurationService._normalized_label(candidate.label or candidate.name) == desired_label
            )
            if not candidate_matches:
                continue
            if ConfigurationService._same_field(candidate, desired, existing_picklist_names, desired_picklist_names):
                return candidate
        return None

    @staticmethod
    def _find_conflicting_field(existing: list[FieldConfiguration], desired_name: str) -> FieldConfiguration | None:
        desired_request_name = ConfigurationService._request_field_name(desired_name).casefold()
        for candidate in existing:
            if ConfigurationService._request_field_name(candidate.name).casefold() == desired_request_name:
                return candidate
        return None

    @staticmethod
    def _available_field_name(name: str, existing: dict[str, FieldConfiguration]) -> str:
        base_name = ConfigurationService._request_field_name(name)
        candidate = base_name + "1"
        suffix = 1
        while candidate.casefold() in existing:
            suffix += 1
            candidate = f"{base_name}{suffix}"
        return candidate

    @staticmethod
    def _rule_key(rule: RelationshipRuleConfiguration) -> tuple[Any, ...]:
        return (rule.fromItemTypeName or rule.fromItemTypeId, rule.toItemTypeName or rule.toItemTypeId, rule.relationshipTypeName or rule.relationshipTypeId, rule.forCoverage)

    def _create_picklist(self, project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.request("picklists", method="POST", data={"project": project_id, "name": payload["name"]})

    def _create_picklist_option(self, picklist_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.request(f"picklists/{picklist_id}/options", method="POST", data={"name": payload["name"], "default": payload.get("default", False)})

    def _create_item_type(self, project_id: int, payload: dict[str, Any], picklists: dict[str, int]) -> dict[str, Any]:
        fields = [
            request_payload
            for value in payload.get("fields", [])
            for request_payload, _ in [self._field_payload(value, picklists)]
            if request_payload is not None
        ]
        display_name = str(payload.get("display") or payload["name"])
        type_key = self._request_item_type_key(str(payload.get("typeKey") or payload["name"]))
        return self.client.request(
            "itemtypes",
            method="POST",
            data={
                "project": project_id,
                "name": display_name,
                "typeKey": type_key,
                "display": display_name,
                "displayPlural": str(payload.get("displayPlural") or self._display_plural(display_name)),
                "fields": fields,
            },
        )

    def _create_item_type_with_unique_key(
        self,
        project_id: int,
        payload: dict[str, Any],
        picklists: dict[str, int],
        existing_keys: set[str],
    ) -> tuple[dict[str, Any], str]:
        candidate_payload = dict(payload)
        desired_key = self._request_item_type_key(str(candidate_payload.get("typeKey") or candidate_payload["name"]))
        desired_key = self._next_available_item_type_key(desired_key, existing_keys)
        candidate_payload["typeKey"] = desired_key
        while True:
            try:
                result = self._create_item_type(project_id, candidate_payload, picklists)
                return result, str(candidate_payload["typeKey"])
            except JamaError as exc:
                if not self._is_duplicate_item_type_key_error(exc):
                    raise
                next_key = self._next_available_item_type_key(
                    self._next_item_type_key(str(candidate_payload["typeKey"])),
                    existing_keys,
                )
                self.logger.warning(
                    "Item type key '%s' already exists; retrying item type '%s' with '%s'.",
                    candidate_payload["typeKey"],
                    candidate_payload.get("name", ""),
                    next_key,
                )
                candidate_payload["typeKey"] = next_key

    def _create_field(
        self,
        item_type_id: int,
        payload: dict[str, Any],
        picklists: dict[str, int],
        *,
        request_ready: bool = False,
    ) -> dict[str, Any]:
        request_payload = payload if request_ready else self._field_payload(payload, picklists)[0]
        if request_payload is None:
            raise JamaError("Field cannot be created automatically.")
        return self.client.request(f"itemtypes/{item_type_id}/fields", method="POST", data=request_payload)

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

    def _existing_item_type_keys(self) -> set[str]:
        if self.client is None:
            return set()
        keys: set[str] = set()
        try:
            for row in self.client.paged("itemtypes"):
                type_key = row.get("typeKey")
                if type_key:
                    keys.add(str(type_key).casefold())
        except JamaError as exc:
            self.logger.warning("Existing item type keys unavailable: %s", exc)
        return keys

    def _find_picklist_id(self, project_id: int, name: str) -> int | None:
        if self.client is None:
            return None
        try:
            for row in self.client.paged("picklists", {"project": project_id}):
                if str(_field_value(row, "name", "")).casefold() == name.casefold() and row.get("id") is not None:
                    return int(row["id"])
        except JamaError:
            return None
        return None

    def _find_item_type_id(self, project_id: int, name: str) -> int | None:
        if self.client is None:
            return None
        try:
            for row in self._get_item_types(project_id):
                if row.name.casefold() == name.casefold() and row.id is not None:
                    return int(row.id)
        except JamaError:
            return None
        return None

    def _get_item_type_fields(self, item_type_id: int) -> list[FieldConfiguration]:
        if self.client is None:
            return []
        try:
            detail = self.client.request(f"itemtypes/{item_type_id}")
        except JamaError as exc:
            self.logger.warning("Created item type fields unavailable: %s", exc)
            return []
        raw_fields = detail.get("fields", [])
        if isinstance(raw_fields, dict):
            raw_fields = raw_fields.get("fields", [])
        return [self._parse_field(value) for value in raw_fields if isinstance(value, dict) and value.get("name")]

    @staticmethod
    def _resolve_created_id(result: Any) -> int | None:
        if isinstance(result, dict):
            if result.get("id") is not None:
                return int(result["id"])
            data = result.get("data")
            if isinstance(data, dict) and data.get("id") is not None:
                return int(data["id"])
            meta = result.get("meta")
            if isinstance(meta, dict) and meta.get("id") is not None:
                return int(meta["id"])
        return None

    @staticmethod
    def _comparable_field_type(field_type: str | None) -> str:
        normalized = str(field_type or "STRING").upper()
        if normalized in {"USER", "DOCUMENT_TYPE_ITEM_LOOKUP"}:
            return "STRING"
        if normalized == "PICKLIST":
            return "LOOKUP"
        return normalized

    @staticmethod
    def _request_field_type(field_type: str | None) -> str | None:
        raw_type = str(field_type or "STRING").upper()
        if raw_type in {"CALCULATED", "DOCUMENT_TYPE_ITEM_LOOKUP"}:
            return None
        normalized = ConfigurationService._comparable_field_type(raw_type)
        if normalized in SUPPORTED_REQUEST_FIELD_TYPES:
            return normalized
        return None

    @staticmethod
    def _request_field_name(name: str) -> str:
        candidate = re.sub(r"\$\d+$", "", name)
        candidate = re.sub(r"[^0-9A-Za-z_]", "_", candidate).strip("_")
        candidate = re.sub(r"_+", "_", candidate)
        if not candidate:
            return "field_value"
        if candidate[0].isdigit():
            return f"field_{candidate}"
        return candidate

    @staticmethod
    def _request_item_type_key(name: str) -> str:
        candidate = re.sub(r"[^0-9A-Za-z_]", "_", name).strip("_")
        candidate = re.sub(r"_+", "_", candidate).upper()
        if not candidate:
            candidate = "ITEM_TYPE"
        if candidate[0].isdigit():
            candidate = f"I_{candidate}"
        if len(candidate) <= 16:
            return candidate
        parts = [part[:3] for part in candidate.split("_") if part]
        shortened = "_".join(parts)[:16].rstrip("_")
        if shortened:
            return shortened
        return candidate[:16].rstrip("_") or "ITEM_TYPE"

    @staticmethod
    def _next_item_type_key(type_key: str) -> str:
        candidate = type_key + "1"
        if len(candidate) <= 16:
            return candidate
        return (type_key[:15] + "1") if type_key else "ITEM_TYPE1"

    @staticmethod
    def _next_available_item_type_key(type_key: str, existing_keys: set[str]) -> str:
        candidate = type_key
        while candidate.casefold() in existing_keys:
            candidate = ConfigurationService._next_item_type_key(candidate)
        return candidate

    @staticmethod
    def _is_duplicate_item_type_key_error(exc: Exception) -> bool:
        message = str(exc)
        return "Item Type Key:" in message and "already being used" in message

    @staticmethod
    def _display_plural(name: str) -> str:
        if not name:
            return name
        lowered = name.casefold()
        if lowered.endswith(("s", "x", "z", "ch", "sh")):
            return name + "es"
        if len(name) > 1 and lowered.endswith("y") and lowered[-2] not in "aeiou":
            return name[:-1] + "ies"
        return name + "s"

    def _field_change_payload(
        self,
        field: FieldConfiguration,
        desired_picklist_names: dict[int, str] | None = None,
        target_picklists: dict[str, PicklistConfiguration] | None = None,
    ) -> dict[str, Any]:
        payload = asdict(field)
        payload["name"] = self._request_field_name(field.name)
        if not payload.get("label"):
            payload["label"] = field.label or field.name
        picklist_id = _int_or_none(field.picklist)
        if not payload.get("picklistName") and picklist_id is not None and desired_picklist_names:
            payload["picklistName"] = desired_picklist_names.get(picklist_id)
        picklist_name = payload.get("picklistName")
        if picklist_name and target_picklists:
            target_picklist = target_picklists.get(str(picklist_name).casefold())
            if target_picklist and target_picklist.id is not None:
                target_picklist_id = target_picklist.id
                payload["targetPicklistId"] = int(target_picklist_id)
        return payload

    @staticmethod
    def _field_payload(payload: dict[str, Any], picklists: dict[str, int]) -> tuple[dict[str, Any] | None, str | None]:
        field_type = ConfigurationService._request_field_type(payload.get("fieldType"))
        if field_type is None:
            return None, f"Field type '{payload.get('fieldType', 'STRING')}' is not supported by the Jama item type field creation API."
        allowed = {"label", "readOnly", "readOnlyAllowApiOverwrite", "required", "triggerSuspect", "synchronize", "textType", "infotip"}
        result = {key: value for key, value in payload.items() if key in allowed and value is not None}
        result["name"] = ConfigurationService._request_field_name(str(payload.get("name", "field_value")))
        result["fieldType"] = field_type
        picklist_name = payload.get("picklistName")
        target_picklist_id = _int_or_none(payload.get("targetPicklistId"))
        if target_picklist_id is None and picklist_name and str(picklist_name).casefold() in picklists:
            target_picklist_id = picklists[str(picklist_name).casefold()]
        if target_picklist_id is None:
            target_picklist_id = _int_or_none(payload.get("pickList"))
        if target_picklist_id is not None:
            result["pickList"] = int(target_picklist_id)
        elif field_type in {"LOOKUP", "MULTI_LOOKUP"}:
            return None, f"Lookup field '{result['name']}' does not have a matching target picklist ID."
        return result, None

    @staticmethod
    def _normalized_label(label: str) -> str:
        return re.sub(r"\s+", " ", label).strip().casefold()

    def _resolve_associated_item_type(
        self,
        item_type: ItemTypeConfiguration,
        current_types_by_name: dict[str, ItemTypeConfiguration],
        current_types_by_id: dict[int, ItemTypeConfiguration],
        instance_types_by_name: dict[str, ItemTypeConfiguration],
        instance_types_by_id: dict[int, ItemTypeConfiguration],
    ) -> tuple[ItemTypeConfiguration | None, bool]:
        associated_id = _int_or_none(item_type.associatedItemTypeId)
        associated_name = (item_type.associatedItemTypeName or "").strip()
        if associated_id is None and not associated_name:
            return None, False
        if associated_id is not None and associated_id in current_types_by_id:
            return current_types_by_id[associated_id], False
        if associated_name:
            current_match = current_types_by_name.get(associated_name.casefold())
            if current_match is not None:
                return current_match, False
        if associated_id is not None and associated_id in instance_types_by_id:
            return instance_types_by_id[associated_id], True
        if associated_name:
            instance_match = instance_types_by_name.get(associated_name.casefold())
            if instance_match is not None:
                return instance_match, True
        return None, False

    def _append_field_changes_for_mapped_item_type(
        self,
        *,
        changes: list[Change],
        source_item_type: ItemTypeConfiguration,
        target_item_type: ItemTypeConfiguration,
        target_picklist_names: dict[int, str],
        desired_picklist_names: dict[int, str],
        target_picklists: dict[str, PicklistConfiguration],
        kind: str,
    ) -> None:
        target_fields = {row.name.casefold(): row for row in target_item_type.fields}
        mapped_message = (
            ""
            if source_item_type.name.casefold() == target_item_type.name.casefold()
            else f" (mapped from '{source_item_type.name}')"
        )
        for config_field in source_item_type.fields:
            candidate = target_fields.get(config_field.name.casefold())
            equivalent = candidate or self._find_equivalent_field(
                target_item_type.fields,
                config_field,
                target_picklist_names,
                desired_picklist_names,
            )
            if equivalent and self._same_field(equivalent, config_field, target_picklist_names, desired_picklist_names):
                continue
            field_payload = self._field_change_payload(config_field, desired_picklist_names, target_picklists) | {
                "itemTypeName": target_item_type.name,
                "targetItemTypeId": target_item_type.id,
                "sourceItemTypeName": source_item_type.name,
            }
            conflict = self._find_conflicting_field(target_item_type.fields, field_payload["name"])
            if conflict:
                if self._same_field(conflict, config_field, target_picklist_names, desired_picklist_names):
                    continue
                conflict_payload = dict(field_payload)
                conflict_payload["existingFieldName"] = conflict.name
                conflict_payload["existingFieldLabel"] = conflict.label
                changes.append(Change(
                    "field_notice",
                    f"field-conflict-mapped:{source_item_type.name}:{target_item_type.name}:{config_field.name}",
                    f"Manual field configuration required for '{field_payload['label']}' on '{target_item_type.name}': field name '{field_payload['name']}' already exists.{mapped_message}",
                    conflict_payload,
                ))
                continue
            if self._request_field_type(config_field.fieldType) is None:
                changes.append(Change(
                    "field_notice",
                    f"field-notice-mapped:{source_item_type.name}:{target_item_type.name}:{config_field.name}",
                    f"Manual field configuration required for '{field_payload['label']}' on '{target_item_type.name}'.{mapped_message}",
                    field_payload,
                ))
                continue
            if kind == "instance_field":
                message = f"Add field '{field_payload['name']}' with '{config_field.fieldType}' to existing Jama Connect item type '{target_item_type.name}'{mapped_message}"
            else:
                message = f"Add field '{field_payload['name']}' to '{target_item_type.name}'{mapped_message}"
            changes.append(Change(
                kind,
                f"{kind}-mapped:{source_item_type.name}:{target_item_type.name}:{config_field.name}",
                message,
                field_payload,
            ))

    def _append_field_changes_for_new_item_type(
        self,
        *,
        changes: list[Change],
        item_type: ItemTypeConfiguration,
        desired_picklist_names: dict[int, str],
        target_picklists: dict[str, PicklistConfiguration],
    ) -> None:
        for config_field in item_type.fields:
            field_payload = self._field_change_payload(config_field, desired_picklist_names, target_picklists) | {
                "itemTypeName": item_type.name,
            }
            if self._is_auto_created_new_item_type_field(config_field, field_payload):
                continue
            if self._request_field_type(config_field.fieldType) is None:
                changes.append(Change(
                    "field_notice",
                    f"field-notice-new:{item_type.name}:{config_field.name}",
                    f"Manual field configuration required for ‘{field_payload['label']}’ on ‘{item_type.name}’",
                    field_payload,
                ))
                continue
            changes.append(Change(
                "instance_field",
                f"instance-field-new:{item_type.name}:{config_field.name}",
                f"Add field '{field_payload['name']}' with '{config_field.fieldType}' to '{item_type.name}'",
                field_payload,
            ))

    @staticmethod
    def _is_auto_created_new_item_type_field(field: FieldConfiguration, field_payload: dict[str, Any]) -> bool:
        field_name = str(field_payload.get("name") or field.name or "").strip().casefold()
        if field_name in AUTO_CREATED_NEW_ITEM_TYPE_FIELD_NAMES:
            return True
        label = str(field_payload.get("label") or field.label or field.name or "")
        return ConfigurationService._normalized_label(label) in AUTO_CREATED_NEW_ITEM_TYPE_FIELD_LABELS

    @staticmethod
    def _recoverable_field_apply_notice(change: Change, exc: Exception) -> str | None:
        message = str(exc)
        if "Field name must be unique for this type." in message:
            field_name = str(change.payload.get("name") or "field")
            item_type_name = str(change.payload.get("itemTypeName") or "item type")
            return f"Skipped '{field_name}' on '{item_type_name}' because the target item type already has a field using that name. Review the existing field manually to satisfy uniqueness requirements."
        return None

