from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ValidationError

FORMAT_NAME = "jama-project-admin-configuration"
FORMAT_VERSION = 1


@dataclass
class FieldConfiguration:
    name: str
    label: str = ""
    fieldType: str = "STRING"
    readOnly: bool = False
    readOnlyAllowApiOverwrite: bool = False
    required: bool = False
    triggerSuspect: bool = False
    synchronize: bool = False
    picklist: int | None = None
    picklistName: str | None = None
    textType: str | None = None
    infotip: str | None = None


@dataclass
class ItemTypeConfiguration:
    id: int | None
    name: str
    fields: list[FieldConfiguration] = field(default_factory=list)


@dataclass
class PicklistOptionConfiguration:
    id: int | None
    name: str
    default: bool = False


@dataclass
class PicklistConfiguration:
    id: int | None
    name: str
    options: list[PicklistOptionConfiguration] = field(default_factory=list)


@dataclass
class RelationshipRuleConfiguration:
    id: int | None
    fromItemTypeId: int | None
    toItemTypeId: int | None
    forCoverage: bool
    relationshipTypeId: int | None
    fromItemTypeName: str | None = None
    toItemTypeName: str | None = None
    relationshipTypeName: str | None = None


@dataclass
class UserConfiguration:
    id: int | None
    username: str
    email: str = ""
    firstName: str = ""
    lastName: str = ""
    active: bool = True


@dataclass
class JamaConfiguration:
    source: dict[str, Any]
    itemTypes: list[ItemTypeConfiguration]
    picklists: list[PicklistConfiguration]
    relationshipRules: list[RelationshipRuleConfiguration]
    users: list[UserConfiguration] = field(default_factory=list)
    format: str = FORMAT_NAME
    version: int = FORMAT_VERSION
    exportedAt: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "JamaConfiguration":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Unable to read configuration file: {exc}") from exc
        if raw.get("format") != FORMAT_NAME or raw.get("version") != FORMAT_VERSION:
            raise ValidationError("Unsupported configuration file format or version.")
        try:
            item_types = [
                ItemTypeConfiguration(
                    id=row.get("id"),
                    name=row["name"],
                    fields=[FieldConfiguration(**value) for value in row.get("fields", [])],
                )
                for row in raw.get("itemTypes", [])
            ]
            picklists = [
                PicklistConfiguration(
                    id=row.get("id"),
                    name=row["name"],
                    options=[PicklistOptionConfiguration(**value) for value in row.get("options", [])],
                )
                for row in raw.get("picklists", [])
            ]
            rules = [RelationshipRuleConfiguration(**row) for row in raw.get("relationshipRules", [])]
            users = [UserConfiguration(**row) for row in raw.get("users", [])]
            return cls(
                source=raw.get("source", {}),
                itemTypes=item_types,
                picklists=picklists,
                relationshipRules=rules,
                users=users,
                exportedAt=raw.get("exportedAt", ""),
            )
        except (KeyError, TypeError) as exc:
            raise ValidationError(f"Malformed configuration file: {exc}") from exc
