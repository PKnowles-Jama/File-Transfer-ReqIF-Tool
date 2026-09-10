import json
import unittest
from pathlib import Path

from ftr_tool.configuration import FieldConfiguration, ItemTypeConfiguration, JamaConfiguration
from ftr_tool.errors import ValidationError


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.path = Path.cwd() / ".test-configuration.json"

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_round_trip(self):
        config = JamaConfiguration(
            source={"projectId": 1, "projectName": "Demo"},
            itemTypes=[
                ItemTypeConfiguration(
                    2,
                    "Requirement",
                    [FieldConfiguration("description", fieldType="TEXT")],
                    typeKey="REQ",
                    display="Requirement",
                    displayPlural="Requirements",
                    associatedItemTypeName="System Requirement",
                    associatedItemTypeId=45,
                )
            ],
            picklists=[], relationshipRules=[],
        )
        config.save(self.path)
        loaded = JamaConfiguration.load(self.path)
        self.assertEqual("Requirement", loaded.itemTypes[0].name)
        self.assertEqual("description", loaded.itemTypes[0].fields[0].name)
        self.assertEqual("REQ", loaded.itemTypes[0].typeKey)
        self.assertEqual("Requirement", loaded.itemTypes[0].display)
        self.assertEqual("Requirements", loaded.itemTypes[0].displayPlural)
        self.assertEqual("System Requirement", loaded.itemTypes[0].associatedItemTypeName)
        self.assertEqual(45, loaded.itemTypes[0].associatedItemTypeId)

    def test_rejects_unknown_format(self):
        self.path.write_text(json.dumps({"format": "other", "version": 1}), encoding="utf-8")
        with self.assertRaises(ValidationError):
            JamaConfiguration.load(self.path)


if __name__ == "__main__":
    unittest.main()
