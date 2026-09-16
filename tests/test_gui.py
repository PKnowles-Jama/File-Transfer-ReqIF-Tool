import unittest
from pathlib import Path
from typing import cast

from ftr_tool.configuration import (
    FieldConfiguration,
    ItemTypeConfiguration,
    JamaConfiguration,
    PicklistConfiguration,
    PicklistOptionConfiguration,
)
from ftr_tool.gui import Application, ImportWindow
from ftr_tool.service import Change, ConfigurationService


class _Project:
    def __init__(self, project_id, key, name):
        self.id = project_id
        self.key = key
        self.name = name


class _InstanceItemTypeClientStub:
    def paged(self, path, query=None):
        if path == "itemtypes":
            return [{"id": 100, "display": "Requirement"}]
        return []

    def request(self, path, method="GET", query=None, data=None):
        if path == "itemtypes/100":
            return {"id": 100, "display": "Requirement", "fields": []}
        raise AssertionError(f"Unexpected request path: {path}")


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ApplicationBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.app = cast(Application, object.__new__(Application))
        self.app.projects = [_Project(102, "FTR", "File Transfer"), _Project(103, "ALT", "Alt Project")]
        self.app.selected_project = None
        self.app.current = None
        self.app.info_project = _Var("")
        self.app.info_current = _Var("")

    def test_default_project_returns_first_when_no_project_context_selected(self):
        project = self.app._default_project()

        self.assertEqual(102, project.id)
        self.assertEqual("FTR", project.key)

    def test_set_selected_project_updates_project_context_summary(self):
        self.app._set_selected_project(self.app.projects[1])

        self.assertEqual(self.app.projects[1], self.app.selected_project)
        self.assertEqual("Project context: Alt Project (key ALT, ID 103)", self.app.info_project.get())


class ImportWindowBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.window = cast(ImportWindow, object.__new__(ImportWindow))
        self.window.service = ConfigurationService(client=None)  # type: ignore[arg-type]
        self.window.current = JamaConfiguration({}, [], [], [])
        self.window.working = JamaConfiguration({}, [], [], [])
        self.window.picklist_targets = {}
        self.window.item_type_targets = {}
        self.window.picklist_controls = {}
        self.window.item_type_controls = {}
        self.window.existing_picklist_names = []
        self.window.existing_item_type_names = []
        self.window.project = _Project(102, "FTR", "File Transfer")
        self.window.import_path = Path("imported-config.json")

    def test_resolved_picklist_selection_defaults_to_exact_existing_target(self):
        self.window.picklist_targets = {
            "priority": (PicklistConfiguration(40, "Priority", []), False),
        }
        self.window.picklist_controls = {
            "Priority": {"selected": _Var(self.window.PICKLIST_PLACEHOLDER)},
        }

        self.assertEqual("Priority", self.window._resolved_picklist_selection("Priority"))
        self.assertFalse(self.window._uses_manual_picklist_mapping("Priority", "Priority"))
        self.assertTrue(self.window._uses_manual_picklist_mapping("Priority", "Risk Priority"))

    def test_picklist_overview_rows_report_exact_existing_target_and_missing_options(self):
        self.window.working = JamaConfiguration(
            {},
            [],
            [PicklistConfiguration(20, "Priority", [PicklistOptionConfiguration(21, "High"), PicklistOptionConfiguration(22, "Medium")])],
            [],
        )
        self.window.picklist_targets = {
            "priority": (
                PicklistConfiguration(40, "Priority", [PicklistOptionConfiguration(41, "High")]),
                False,
            ),
        }

        rows = self.window._picklist_overview_rows()

        self.assertEqual(1, len(rows))
        self.assertEqual("Priority", rows[0][0].name)
        self.assertEqual("Priority", rows[0][1].name)
        self.assertEqual(["Medium"], rows[0][3])

    def test_build_picklist_changes_uses_exact_existing_target_without_manual_mapping(self):
        self.window.working = JamaConfiguration(
            {},
            [],
            [PicklistConfiguration(20, "Priority", [PicklistOptionConfiguration(21, "High"), PicklistOptionConfiguration(22, "Medium")])],
            [],
        )
        self.window.picklist_targets = {
            "priority": (
                PicklistConfiguration(40, "Priority", [PicklistOptionConfiguration(41, "High")]),
                False,
            ),
        }

        changes = self.window._build_picklist_changes()

        self.assertEqual(["instance_picklist_notice", "instance_picklist_option"], [row.kind for row in changes])
        self.assertEqual("Priority", changes[1].payload["picklistName"])
        self.assertEqual(40, changes[1].payload["targetPicklistId"])

    def test_set_picklist_option_selections_updates_all_visible_options(self):
        first = _Var(True)
        second = _Var(True)
        self.window.picklist_controls = {
            "Priority": {"option_vars": {"High": first, "Medium": second}},
        }

        self.window._set_picklist_option_selections(False)

        self.assertFalse(first.get())
        self.assertFalse(second.get())

    def test_set_picklist_mapping_enabled_updates_each_control_and_refreshes_details(self):
        refreshed = []
        self.window.picklist_controls = {
            "Priority": {"enabled": _Var(True)},
            "Risk": {"enabled": _Var(True)},
        }
        self.window._refresh_picklist_detail = lambda name: refreshed.append(name)

        self.window._set_picklist_mapping_enabled(False)

        self.assertFalse(self.window.picklist_controls["Priority"]["enabled"].get())
        self.assertFalse(self.window.picklist_controls["Risk"]["enabled"].get())
        self.assertEqual(["Priority", "Risk"], refreshed)

    def test_configured_working_copy_does_not_create_manual_association_for_exact_item_type_match(self):
        self.window.working = JamaConfiguration(
            {},
            [ItemTypeConfiguration(10, "Requirement", [FieldConfiguration("priority")])],
            [],
            [],
        )
        self.window.item_type_targets = {
            "requirement": (ItemTypeConfiguration(30, "Requirement", []), False),
        }

        configured = self.window._configured_working_copy_for_item_types()

        self.assertIsNone(configured.itemTypes[0].associatedItemTypeId)
        self.assertIsNone(configured.itemTypes[0].associatedItemTypeName)

    def test_item_type_overview_rows_report_exact_existing_target_and_missing_fields(self):
        self.window.current = JamaConfiguration(
            {},
            [ItemTypeConfiguration(30, "Requirement", [])],
            [],
            [],
        )
        self.window.working = JamaConfiguration(
            {},
            [ItemTypeConfiguration(10, "Requirement", [FieldConfiguration("priority", fieldType="STRING")])],
            [],
            [],
        )
        self.window.item_type_targets = {
            "requirement": (ItemTypeConfiguration(30, "Requirement", []), True),
        }

        rows = self.window._item_type_overview_rows()

        self.assertEqual(1, len(rows))
        self.assertEqual("Requirement", rows[0][0].name)
        self.assertEqual("Requirement", rows[0][1].name)
        self.assertEqual(["field"], [change.kind for change in rows[0][3]])

    def test_configured_working_copy_keeps_manual_association_for_different_item_type_name(self):
        self.window.working = JamaConfiguration(
            {},
            [ItemTypeConfiguration(10, "System Requirement", [FieldConfiguration("priority")])],
            [],
            [],
        )
        self.window.item_type_targets = {
            "requirement": (ItemTypeConfiguration(30, "Requirement", []), True),
        }
        self.window.item_type_controls = {
            "System Requirement": {
                "enabled": _Var(True),
                "selected": _Var("Requirement"),
            },
        }

        configured = self.window._configured_working_copy_for_item_types()

        self.assertEqual(30, configured.itemTypes[0].associatedItemTypeId)
        self.assertEqual("Requirement", configured.itemTypes[0].associatedItemTypeName)

    def test_set_item_type_field_selections_updates_all_visible_fields(self):
        first = _Var(True)
        second = _Var(True)
        self.window.item_type_controls = {
            "Requirement": {"field_vars": {"field:one": first, "field:two": second}},
        }

        self.window._set_item_type_field_selections(False)

        self.assertFalse(first.get())
        self.assertFalse(second.get())

    def test_set_item_type_mapping_enabled_updates_each_control_and_refreshes_details(self):
        refreshed = []
        self.window.item_type_controls = {
            "Requirement": {"enabled": _Var(True)},
            "Risk": {"enabled": _Var(True)},
        }
        self.window._refresh_item_type_detail = lambda name: refreshed.append(name)

        self.window._set_item_type_mapping_enabled(False)

        self.assertFalse(self.window.item_type_controls["Requirement"]["enabled"].get())
        self.assertFalse(self.window.item_type_controls["Risk"]["enabled"].get())
        self.assertEqual(["Requirement", "Risk"], refreshed)

    def test_deselect_all_item_types_turns_off_all_item_type_controls(self):
        self.window.item_type_controls = {
            "Requirement": {"enabled": _Var(True)},
            "Risk": {"enabled": _Var(True)},
        }
        self.window._refresh_item_type_detail = lambda name: None

        self.window._deselect_all_item_types()

        self.assertFalse(self.window.item_type_controls["Requirement"]["enabled"].get())
        self.assertFalse(self.window.item_type_controls["Risk"]["enabled"].get())

    def test_select_all_item_types_turns_on_all_item_type_controls(self):
        self.window.item_type_controls = {
            "Requirement": {"enabled": _Var(False)},
            "Risk": {"enabled": _Var(False)},
        }
        self.window._refresh_item_type_detail = lambda name: None

        self.window._select_all_item_types()

        self.assertTrue(self.window.item_type_controls["Requirement"]["enabled"].get())
        self.assertTrue(self.window.item_type_controls["Risk"]["enabled"].get())

    def test_project_configuration_item_type_names_include_created_and_existing_instance_types_once(self):
        changes = [
            Change("instance_item_type_notice", "notice:Requirement", "Existing type needs project configuration", {"itemTypeName": "Requirement", "instanceItemTypeId": 100}),
            Change("item_type", "type:Custom Risk", "Create item type", {"name": "Custom Risk"}),
            Change("instance_item_type_notice", "notice:requirement-duplicate", "Duplicate notice", {"itemTypeName": "Requirement", "instanceItemTypeId": 100}),
        ]

        self.assertEqual(["Requirement", "Custom Risk"], self.window._project_configuration_item_type_names(changes))

    def test_skipped_item_type_names_report_disabled_controls(self):
        self.window.item_type_controls = {
            "Requirement": {"enabled": _Var(True)},
            "Custom Risk": {"enabled": _Var(False)},
            "System Requirement": {"enabled": _Var(False)},
        }

        self.assertEqual(["Custom Risk", "System Requirement"], self.window._skipped_item_type_names())

    def test_project_configuration_updates_text_includes_api_ids_and_skipped_item_types(self):
        self.window.service = ConfigurationService(client=_InstanceItemTypeClientStub())  # type: ignore[arg-type]
        self.window.item_type_controls = {
            "Skipped Type": {"enabled": _Var(False)},
        }
        changes = [
            Change("instance_item_type_notice", "notice:Requirement", "Existing type needs project configuration", {"itemTypeName": "Requirement", "instanceItemTypeId": 100}),
        ]

        text = self.window._project_configuration_updates_text(changes)

        self.assertIn("Configuration Follow-Up", text)
        self.assertIn("- Requirement (API ID 100)", text)
        self.assertIn("- Skipped Type (skipped during import; not mapped to an existing item type and not created as a new item type)", text)
        self.assertNotIn("Target project:", text)


if __name__ == "__main__":
    unittest.main()


