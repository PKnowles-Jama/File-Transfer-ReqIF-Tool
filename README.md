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
2. Select a project.
3. Select **Load project configuration**.
4. Choose either:
   - **Export configuration** to save a portable JSON file in the current directory, or
   - **Choose import file**, review the proposed changes, select the desired changes, and choose **Apply selected changes**.

Import changes execute in the required dependency order: picklists, picklist options, item types, then fields. Relationship-rule gaps are reported for manual TIM configuration because Jama installations differ in whether their public REST API permits relationship-rule administration.

Logs are saved under `logs/` in the current directory.

## Tests

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

