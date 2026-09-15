# File Transfer ReqIF Tool

A dependency-free Python desktop application for moving Jama Connect administrative configuration between projects. It supports Basic and OAuth client-credentials authentication, project selection, JSON configuration export, configuration comparison, ordered import, readable progress, and persistent logs.

## Run

Python 3.10+ with Tk support is required.

```powershell
python -m ftr_tool
```

From a source checkout without installation:

```powershell
$env:PYTHONPATH = "src"
python -m ftr_tool
```

Credentials are held in memory only and are never written to the configuration file or logs. Use an account with the Jama permissions required for project-administration changes.

## Workflow

1. Enter the Jama base URL, choose Basic or OAuth, enter credentials, and select **Authenticate**.
   - After authentication, the URL is locked. Use **Update URL** to unlock it, then **Submit URL** to confirm the new value before re-authenticating.
2. Select a project.
3. Select **Load project configuration**.
4. Choose either:
   - **Export configuration** to save a portable JSON file in the current directory, or
   - **Choose import file**, review the proposed changes, select the desired changes, and choose **Apply selected changes**.
   - **Export selected (Yes) changes** to save an updated configuration file that keeps only changes currently marked **Yes**.

The **Relevant info** section in the GUI shows live context while you work, including the authenticated Jama Connect URL, auth mode, selected project (key and ID), loaded target configuration counts, import filename and metadata, and selected actionable-change totals with notice counts.

When **Update URL** is selected or the authentication mode is changed, the tool clears credential entries, project selection, and previously listed configuration changes so stale values are not reused.

When a picklist in the import file is not present in the current project, compare now checks whether the picklist exists in the wider Jama Connect instance and shows guidance and option-add prompts accordingly.

Field comparison uses field type plus normalized field-name matching and picklist-name equivalence (instead of relying on picklist IDs that differ between instances).

Field-name uniqueness is enforced conservatively during compare/import: if the target item type already uses the sanitized Jama field name for a non-equivalent field, the tool now reports a manual-review notice instead of auto-posting a duplicate field create request.

When an imported item type does not exist in the target project or instance, the compare flow now prompts you to optionally map it to an existing target item type. If mapped, the tool treats that mapped type as the comparison target for missing fields. If not mapped, the new item type is created first and each of its fields is shown as a separate Yes/No prompt so the item type and its fields can be completed in the same run. You can still predefine a mapping in JSON with `associatedItemTypeName` or `associatedItemTypeId` on the `itemTypes[]` entry.

For best results after upgrading the tool, regenerate source configuration exports before importing them elsewhere so the JSON includes preserved item type key/display metadata used when creating new item types.

Import changes execute in the required dependency order: picklists, picklist options, item types, then fields. Relationship-rule gaps are reported for manual TIM configuration because Jama installations differ in whether their public REST API permits relationship-rule administration.

Logs are saved under `logs/` in the current directory.

## Tests

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

