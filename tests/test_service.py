import unittest

from ftr_tool.configuration import (
    FieldConfiguration, ItemTypeConfiguration, JamaConfiguration,
    PicklistConfiguration, PicklistOptionConfiguration, UserConfiguration,
)
from ftr_tool.service import Change, ConfigurationService


class _ClientStub:
    def paged(self, path, query=None):
        if path == "itemtypes":
            return [{"id": 100, "display": "Requirement"}]
        if path == "picklists":
            return []
        return []

    def request(self, path, method="GET", query=None, data=None):
        if path == "itemtypes/100":
            return {"id": 100, "display": "Requirement", "fields": []}
        raise AssertionError(f"Unexpected request path: {path}")


class _PicklistClientStub:
    def paged(self, path, query=None):
        if path == "picklists":
            return [{"id": 40, "name": "Priority"}]
        if path == "picklists/40/options":
            return [{"id": 41, "name": "High", "default": False}]
        return []

    def request(self, path, method="GET", query=None, data=None):
        raise AssertionError(f"Unexpected request path: {path}")


class _InstanceRiskPicklistClientStub:
    def paged(self, path, query=None):
        if path == "picklists":
            return [{"id": 501, "name": "Object Type"}, {"id": 601, "name": "Risk Type"}]
        if path == "picklists/501/options":
            return []
        if path == "picklists/601/options":
            return []
        if path == "itemtypes":
            return []
        return []

    def request(self, path, method="GET", query=None, data=None):
        raise AssertionError(f"Unexpected request path: {path}")


class _ApplyClientStub:
    def __init__(self):
        self.calls = []

    def request(self, path, method="GET", query=None, data=None):
        self.calls.append((method, path, data))
        if method == "POST" and path == "picklists":
            return {"meta": {"id": 999}}
        if method == "POST" and path == "picklists/999/options":
            return {"id": 1001}
        if method == "POST" and path == "itemtypes":
            return {"meta": {"id": 888}}
        if method == "POST" and path == "itemtypes/30/fields":
            return {"id": 777}
        raise AssertionError(f"Unexpected request: {method} {path}")

    def paged(self, path, query=None):
        return []


class _TypeKeyCollisionClientStub:
    def __init__(self):
        self.calls = []
        self._attempted_sub = False

    def paged(self, path, query=None):
        if path == "itemtypes":
            return [{"id": 1, "typeKey": "SUB"}, {"id": 2, "typeKey": "TPM"}]
        return []

    def request(self, path, method="GET", query=None, data=None):
        self.calls.append((method, path, data))
        if method == "POST" and path == "itemtypes":
            key = data.get("typeKey")
            if key == "SUB" and not self._attempted_sub:
                self._attempted_sub = True
                from ftr_tool.errors import JamaError

                raise JamaError("Jama returned HTTP 400: Item Type Key: SUB is already being used by Item Type: Subsystem Requirement")
            return {"meta": {"id": 777}}
        raise AssertionError(f"Unexpected request: {method} {path}")


class ConfigurationServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ConfigurationService(client=None)  # type: ignore[arg-type]

    def config(self, item_types=None, picklists=None):
        return JamaConfiguration({}, item_types or [], picklists or [], [])

    def test_compare_orders_discovered_changes_by_kind(self):
        desired = self.config(
            [ItemTypeConfiguration(10, "Requirement", [FieldConfiguration("priority")])],
            [PicklistConfiguration(20, "Priority", [PicklistOptionConfiguration(21, "High")])],
        )
        changes = self.service.compare(desired, self.config())
        self.assertEqual(["picklist", "picklist_option", "item_type"], [row.kind for row in changes])

    def test_adds_missing_option_and_field(self):
        desired = self.config(
            [ItemTypeConfiguration(10, "Requirement", [FieldConfiguration("priority", fieldType="PICKLIST", picklistName="Priority")])],
            [PicklistConfiguration(20, "Priority", [PicklistOptionConfiguration(21, "High")])],
        )
        current = self.config(
            [ItemTypeConfiguration(30, "Requirement", [])],
            [PicklistConfiguration(40, "Priority", [])],
        )
        changes = self.service.compare(desired, current)
        self.assertEqual(["picklist_option", "field"], [row.kind for row in changes])

    def test_user_field_compare_treats_string_as_equivalent(self):
        desired = self.config([ItemTypeConfiguration(1, "Requirement", [FieldConfiguration("owner", fieldType="USER")])])
        current = self.config([ItemTypeConfiguration(2, "Requirement", [FieldConfiguration("owner", fieldType="STRING")])])
        changes = self.service.compare(desired, current)
        self.assertEqual([], changes)

    def test_user_field_exports_as_string(self):
        field = self.service._parse_field({"name": "owner", "fieldType": "USER"})
        self.assertEqual("STRING", field.fieldType)

    def test_missing_project_item_type_uses_instance_notice_and_field_prompt(self):
        service = ConfigurationService(client=_ClientStub())  # type: ignore[arg-type]
        desired = self.config([ItemTypeConfiguration(10, "Requirement", [FieldConfiguration("priority", fieldType="STRING")])])
        changes = service.compare(desired, self.config())
        self.assertEqual(["instance_item_type_notice", "instance_field"], [row.kind for row in changes])

    def test_missing_item_type_can_map_to_existing_project_item_type(self):
        desired = self.config([
            ItemTypeConfiguration(
                10,
                "System Requirement",
                [FieldConfiguration("priority", fieldType="STRING")],
                associatedItemTypeName="Requirement",
            )
        ])
        current = self.config([ItemTypeConfiguration(30, "Requirement", [])])
        changes = self.service.compare(desired, current)
        self.assertEqual(["field"], [row.kind for row in changes])
        self.assertEqual("Requirement", changes[0].payload["itemTypeName"])
        self.assertEqual("System Requirement", changes[0].payload["sourceItemTypeName"])

    def test_missing_item_type_can_map_to_existing_instance_item_type(self):
        service = ConfigurationService(client=_ClientStub())  # type: ignore[arg-type]
        desired = self.config([
            ItemTypeConfiguration(
                10,
                "System Requirement",
                [FieldConfiguration("priority", fieldType="STRING")],
                associatedItemTypeName="Requirement",
            )
        ])
        changes = service.compare(desired, self.config())
        self.assertEqual(["instance_item_type_notice", "instance_field"], [row.kind for row in changes])
        self.assertIn("associated item type Requirement", changes[0].message)
        self.assertEqual("Requirement", changes[1].payload["itemTypeName"])

    def test_association_candidates_include_project_and_instance_types(self):
        service = ConfigurationService(client=_ClientStub())  # type: ignore[arg-type]
        desired = self.config([ItemTypeConfiguration(10, "System Requirement", [FieldConfiguration("priority", fieldType="STRING")])])
        current = self.config([ItemTypeConfiguration(30, "Stakeholder Requirement", [])])
        candidates = service.association_candidates(desired, current)
        self.assertIn("System Requirement", candidates)
        self.assertEqual(
            ["Requirement", "Stakeholder Requirement"],
            [row.name for row in candidates["System Requirement"]],
        )

    def test_apply_item_type_association_sets_and_clears_association_fields(self):
        item_type = ItemTypeConfiguration(10, "System Requirement", [FieldConfiguration("priority")])
        mapped = ItemTypeConfiguration(30, "Requirement", [])
        ConfigurationService.apply_item_type_association(item_type, mapped)
        self.assertEqual(30, item_type.associatedItemTypeId)
        self.assertEqual("Requirement", item_type.associatedItemTypeName)
        ConfigurationService.apply_item_type_association(item_type, None)
        self.assertIsNone(item_type.associatedItemTypeId)
        self.assertIsNone(item_type.associatedItemTypeName)

    def test_export_selected_configuration_only_keeps_yes_changes(self):
        desired = JamaConfiguration(
            source={"projectKey": "SRC"},
            itemTypes=[ItemTypeConfiguration(10, "Requirement", [FieldConfiguration("priority")])],
            picklists=[PicklistConfiguration(20, "Priority", [PicklistOptionConfiguration(21, "High"), PicklistOptionConfiguration(22, "Medium")])],
            relationshipRules=[],
            users=[UserConfiguration(30, "tester")],
        )
        selected = [
            Change("field", "field:Requirement:priority", "Add field", {"name": "priority", "itemTypeName": "Requirement", "fieldType": "STRING"}),
            Change("picklist", "picklist:Priority", "Create picklist", {"name": "Priority"}),
            Change("picklist_option", "option:Priority:High", "Add option", {"name": "High", "picklistName": "Priority"}),
        ]
        exported = self.service.export_selected_configuration(desired, selected)
        self.assertEqual(["Requirement"], [row.name for row in exported.itemTypes])
        self.assertEqual(["priority"], [row.name for row in exported.itemTypes[0].fields])
        self.assertEqual(["Priority"], [row.name for row in exported.picklists])
        self.assertEqual(["High"], [row.name for row in exported.picklists[0].options])
        self.assertEqual([], exported.users)

    def test_missing_project_picklist_includes_option_changes_for_new_picklist(self):
        desired = self.config(picklists=[PicklistConfiguration(20, "Priority", [PicklistOptionConfiguration(21, "High"), PicklistOptionConfiguration(22, "Medium")])])
        changes = self.service.compare(desired, self.config())
        self.assertEqual(["picklist", "picklist_option", "picklist_option"], [row.kind for row in changes])
        self.assertEqual(["High", "Medium"], [row.payload["name"] for row in changes[1:]])

    def test_missing_project_picklist_uses_instance_notice(self):
        service = ConfigurationService(client=_PicklistClientStub())  # type: ignore[arg-type]
        desired = self.config(picklists=[PicklistConfiguration(20, "Priority", [PicklistOptionConfiguration(21, "High")])])
        changes = service.compare(desired, self.config())
        self.assertEqual(["instance_picklist_notice"], [row.kind for row in changes])

    def test_missing_instance_picklist_option_prompts_yes_no_change(self):
        service = ConfigurationService(client=_PicklistClientStub())  # type: ignore[arg-type]
        desired = self.config(picklists=[PicklistConfiguration(20, "Priority", [PicklistOptionConfiguration(21, "High"), PicklistOptionConfiguration(22, "Medium")])])
        changes = service.compare(desired, self.config())
        self.assertEqual(["instance_picklist_notice", "instance_picklist_option"], [row.kind for row in changes])
        self.assertIn("Would you like to add 'Medium' to 'Priority' in Project Configuration?", changes[1].message)

    def test_apply_picklist_uses_meta_id_for_option_creation(self):
        client = _ApplyClientStub()
        service = ConfigurationService(client=client)  # type: ignore[arg-type]
        changes = [
            Change("picklist", "picklist:Priority", "Create picklist", {"name": "Priority"}),
            Change("picklist_option", "option:Priority:High", "Add option", {"picklistName": "Priority", "name": "High", "default": False}),
        ]
        outcomes = service.apply(102, changes)
        self.assertTrue(any(row.startswith("SUCCESS") for row in outcomes))
        self.assertIn(("POST", "picklists/999/options", {"name": "High", "default": False}), client.calls)

    def test_compare_skips_equivalent_lookup_field_when_name_suffix_differs(self):
        desired = self.config(
            [ItemTypeConfiguration(10, "Text", [FieldConfiguration("object_type$33", label="Object Type", fieldType="LOOKUP", picklist=266)])],
            [PicklistConfiguration(266, "Object Type", [])],
        )
        current = self.config(
            [ItemTypeConfiguration(11, "Text", [FieldConfiguration("object_type$125", label="Object Type", fieldType="LOOKUP", picklist=501)])],
            [PicklistConfiguration(501, "Object Type", [])],
        )
        changes = self.service.compare(desired, current)
        self.assertEqual([], [row for row in changes if row.kind == "field"])

    def test_apply_item_type_includes_display_name(self):
        client = _ApplyClientStub()
        service = ConfigurationService(client=client)  # type: ignore[arg-type]
        changes = [Change("item_type", "type:Example", "Create item type", {"name": "Example", "fields": []})]
        outcomes = service.apply(102, changes)
        self.assertTrue(any(row.startswith("SUCCESS") for row in outcomes))
        create_calls = [row for row in client.calls if row[1] == "itemtypes"]
        self.assertEqual(1, len(create_calls))
        self.assertEqual("Example", create_calls[0][2]["display"])
        self.assertEqual("Examples", create_calls[0][2]["displayPlural"])
        self.assertEqual("EXAMPLE", create_calls[0][2]["typeKey"])

    def test_apply_item_type_uses_preserved_type_key(self):
        client = _ApplyClientStub()
        service = ConfigurationService(client=client)  # type: ignore[arg-type]
        changes = [Change("item_type", "type:User Story", "Create item type", {"name": "User Story", "typeKey": "USERSTORY", "fields": []})]
        outcomes = service.apply(102, changes)
        self.assertTrue(any(row.startswith("SUCCESS") for row in outcomes))
        create_calls = [row for row in client.calls if row[1] == "itemtypes"]
        self.assertEqual("USERSTORY", create_calls[0][2]["typeKey"])

    def test_apply_item_type_appends_one_when_type_key_exists(self):
        client = _TypeKeyCollisionClientStub()
        service = ConfigurationService(client=client)  # type: ignore[arg-type]
        changes = [Change("item_type", "type:MS Subsystem Requirement", "Create item type", {"name": "MS Subsystem Requirement", "typeKey": "SUB", "fields": []})]
        outcomes = service.apply(102, changes)
        self.assertTrue(any(row.startswith("SUCCESS") for row in outcomes))
        create_calls = [row for row in client.calls if row[1] == "itemtypes"]
        self.assertEqual(1, len(create_calls))
        self.assertEqual("SUB1", create_calls[0][2]["typeKey"])

    def test_apply_item_type_retries_with_additional_one_after_duplicate_error(self):
        class _RetryClient(_TypeKeyCollisionClientStub):
            def request(self, path, method="GET", query=None, data=None):
                self.calls.append((method, path, data))
                if method == "POST" and path == "itemtypes":
                    key = data.get("typeKey")
                    if key == "SUB1":
                        from ftr_tool.errors import JamaError

                        raise JamaError(f"Jama returned HTTP 400: Item Type Key: {key} is already being used by Item Type: Existing")
                    return {"meta": {"id": 778}}
                raise AssertionError(f"Unexpected request: {method} {path}")

            def paged(self, path, query=None):
                if path == "itemtypes":
                    return [{"id": 1, "typeKey": "SUB"}]
                return []

        client = _RetryClient()
        service = ConfigurationService(client=client)  # type: ignore[arg-type]
        changes = [Change("item_type", "type:MS Subsystem Requirement", "Create item type", {"name": "MS Subsystem Requirement", "typeKey": "SUB", "fields": []})]
        outcomes = service.apply(102, changes)
        self.assertTrue(any(row.startswith("SUCCESS") for row in outcomes))
        create_calls = [row for row in client.calls if row[1] == "itemtypes"]
        self.assertEqual(["SUB1", "SUB11"], [row[2]["typeKey"] for row in create_calls])

    def test_compare_field_payload_uses_target_picklist_id_and_sanitized_name(self):
        desired = self.config(
            [ItemTypeConfiguration(10, "Text", [FieldConfiguration("object_type$33", label="Object Type", fieldType="LOOKUP", picklist=266)])],
            [PicklistConfiguration(266, "Object Type", [])],
        )
        current = self.config(
            [ItemTypeConfiguration(30, "Text", [])],
            [PicklistConfiguration(501, "Object Type", [])],
        )
        changes = self.service.compare(desired, current)
        self.assertEqual(["field"], [row.kind for row in changes])
        self.assertEqual("object_type", changes[0].payload["name"])
        self.assertEqual("Object Type", changes[0].payload["picklistName"])
        self.assertEqual(501, changes[0].payload["targetPicklistId"])

    def test_compare_field_payload_can_use_instance_picklist_id(self):
        service = ConfigurationService(client=_InstanceRiskPicklistClientStub())  # type: ignore[arg-type]
        desired = self.config(
            [ItemTypeConfiguration(10, "Text", [FieldConfiguration("object_type$33", label="Object Type", fieldType="LOOKUP", picklist=266)])],
            [PicklistConfiguration(266, "Object Type", [])],
        )
        current = self.config([ItemTypeConfiguration(30, "Text", [])], [])
        changes = service.compare(desired, current)
        self.assertEqual(["instance_picklist_notice", "field"], [row.kind for row in changes])
        self.assertEqual(501, changes[1].payload["targetPicklistId"])

    def test_compare_new_item_type_preserves_instance_picklist_ids_for_fields(self):
        service = ConfigurationService(client=_InstanceRiskPicklistClientStub())  # type: ignore[arg-type]
        desired = self.config(
            [ItemTypeConfiguration(10, "Custom Risk", [FieldConfiguration("risk_type$125", label="Risk Type", fieldType="LOOKUP", picklist=241)], typeKey="CRISK")],
            [PicklistConfiguration(241, "Risk Type", [])],
        )
        changes = service.compare(desired, self.config())
        self.assertEqual(["instance_picklist_notice", "item_type"], [row.kind for row in changes])
        self.assertEqual(601, changes[1].payload["fields"][0]["targetPicklistId"])

    def test_field_payload_uses_picklist_request_key(self):
        payload, reason = self.service._field_payload(
            {"name": "object_type$33", "fieldType": "LOOKUP", "picklistName": "Object Type", "targetPicklistId": 501},
            {},
        )
        self.assertIsNone(reason)
        if payload is None:
            self.fail("Expected a request payload for the lookup field.")
        self.assertEqual("object_type", payload["name"])
        self.assertEqual(501, payload["pickList"])
        self.assertNotIn("picklist", payload)

    def test_compare_unsupported_field_type_becomes_notice(self):
        desired = self.config([ItemTypeConfiguration(10, "Risk", [FieldConfiguration("risk_score$125", label="Risk Score", fieldType="CALCULATED")])])
        current = self.config([ItemTypeConfiguration(30, "Risk", [])])
        changes = self.service.compare(desired, current)
        self.assertEqual(["field_notice"], [row.kind for row in changes])
        self.assertEqual("risk_score", changes[0].payload["name"])

    def test_apply_unsupported_field_type_returns_notice_without_post(self):
        client = _ApplyClientStub()
        service = ConfigurationService(client=client)  # type: ignore[arg-type]
        changes = [
            Change(
                "field",
                "field:Risk:risk_score$125",
                "Add field ‘risk_score’ to ‘Risk’",
                {"name": "risk_score$125", "label": "Risk Score", "fieldType": "CALCULATED", "itemTypeName": "Risk", "targetItemTypeId": 30},
            )
        ]
        outcomes = service.apply(102, changes)
        self.assertEqual(["NOTICE: Field type 'CALCULATED' is not supported by the Jama item type field creation API."], outcomes)
        self.assertEqual([], [row for row in client.calls if row[1] == "itemtypes/30/fields"])

    def test_compare_skips_equivalent_field_when_label_matches_and_suffix_differs(self):
        desired = self.config(
            [ItemTypeConfiguration(10, "Risk", [FieldConfiguration("scenario$125", label="Scenario", fieldType="STRING")])]
        )
        current = self.config(
            [ItemTypeConfiguration(30, "Risk", [FieldConfiguration("scenario", label="Scenario", fieldType="STRING")])]
        )
        changes = self.service.compare(desired, current)
        self.assertEqual([], [row for row in changes if row.kind in {"field", "field_notice"}])

    def test_compare_conflicting_sanitized_field_name_becomes_notice(self):
        desired = self.config(
            [ItemTypeConfiguration(10, "Risk", [FieldConfiguration("scenario$125", label="Scenario", fieldType="STRING")])]
        )
        current = self.config(
            [ItemTypeConfiguration(30, "Risk", [FieldConfiguration("scenario", label="Scenario", fieldType="TEXT")])]
        )
        changes = self.service.compare(desired, current)
        self.assertEqual(["field_notice"], [row.kind for row in changes])
        self.assertIn("already exists", changes[0].message)

    def test_apply_duplicate_field_name_returns_notice(self):
        client = _ApplyClientStub()
        service = ConfigurationService(client=client)  # type: ignore[arg-type]
        original_create_field = service._create_field

        def duplicate_field(*args, **kwargs):
            raise Exception("Jama returned HTTP 400: Field name must be unique for this type.")

        service._create_field = duplicate_field  # type: ignore[method-assign]
        changes = [
            Change(
                "field",
                "field:Risk:scenario$125",
                "Add field ‘scenario’ to ‘Risk’",
                {"name": "scenario$125", "label": "Scenario", "fieldType": "STRING", "itemTypeName": "Risk", "targetItemTypeId": 30},
            )
        ]
        outcomes = service.apply(102, changes)
        service._create_field = original_create_field  # type: ignore[method-assign]
        self.assertEqual(
            ["NOTICE: Skipped 'scenario$125' on 'Risk' because the target item type already has a field using that name. Review the existing field manually to satisfy uniqueness requirements."],
            outcomes,
        )


if __name__ == "__main__":
    unittest.main()
