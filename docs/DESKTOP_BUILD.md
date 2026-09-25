# Desktop build

On Windows, from the project root run:

```powershell
.\scripts\build_windows.ps1
.\scripts\verify_exe.ps1
```

The first command installs PyInstaller into the active Python environment only if it is missing (PyInstaller is a build-time tool and is not in `requirements.txt`), builds a one-folder bundle, and reports the executable size. The release artifact is `dist\ScriptPlayground\ScriptPlayground.exe`; the verification script starts that exact exe and exercises its HTTP routes, real demo interactions, bundled resources, user-data seeding, parity with Python mode, and graceful Ctrl+Break shutdown. The bundle avoids one-file self-extraction on every launch. Keep build output separate from runtime user data.

Double-click `dist\ScriptPlayground\ScriptPlayground.exe` to run it. For a shortcut, right-click the exe → **Show more options** → **Create shortcut**. Open the shortcut's **Properties → Change Icon…** and select `assets\icon.ico` from the checkout or copy it beside the executable in the bundle.

User files persist outside the exe: Windows uses `%LOCALAPPDATA%\ScriptPlayground\` (scripts, scenarios, designs, and bot workspaces). The first packaged launch seeds built-in scripts/scenarios/designs without replacing existing content. Set `SCRIPTPLAYGROUND_DATA_DIR` or pass `--data-dir PATH` to choose another location. Bundled static UI, Embeder, scripts, and icon are read-only resources; `_MEIPASS` is never used for saved data.

For Linux users running from a source checkout, create `~/.local/share/applications/scriptplayground.desktop` and replace the paths with the checkout's absolute path:

```ini
[Desktop Entry]
Type=Application
Name=ScriptPlayground
Comment=Local Discord bot UI playground
Exec=/absolute/path/to/ScirptPlayground/.venv/bin/python /absolute/path/to/ScirptPlayground/launcher.py
Path=/absolute/path/to/ScirptPlayground
Icon=/absolute/path/to/ScirptPlayground/assets/icon.svg
Terminal=true
Categories=Development;IDE;
```
