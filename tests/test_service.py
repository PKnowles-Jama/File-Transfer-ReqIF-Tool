import unittest

from ftr_tool.configuration import (
    FieldConfiguration, ItemTypeConfiguration, JamaConfiguration,
    PicklistConfiguration, PicklistOptionConfiguration,
)
from ftr_tool.service import ConfigurationService


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
        self.assertEqual(["picklist", "item_type"], [row.kind for row in changes])

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

    def test_conflicting_field_gets_numeric_suffix(self):
        desired = self.config([ItemTypeConfiguration(1, "Requirement", [FieldConfiguration("owner", fieldType="USER")])])
        current = self.config([ItemTypeConfiguration(2, "Requirement", [FieldConfiguration("owner", fieldType="STRING")])])
        changes = self.service.compare(desired, current)
        self.assertEqual("owner1", changes[0].payload["name"])

    def test_user_field_exports_as_string(self):
        field = self.service._parse_field({"name": "owner", "fieldType": "USER"})
        self.assertEqual("STRING", field.fieldType)


if __name__ == "__main__":
    unittest.main()
