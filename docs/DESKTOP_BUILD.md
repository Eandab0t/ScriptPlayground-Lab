# Desktop build

## Electron app (current)

From the project root:

```powershell
npm install
npm run dist
```

`npm run dist` builds the headless server with PyInstaller (`ScriptPlayground-server.spec` → `dist/server-build/server/ScriptPlayground-server.exe`) and then packages the Electron shell with electron-builder. The distributable is **`release\win-unpacked\ScriptPlayground.exe`** (one-folder build; a folder of server resources sits beside it under `resources\server\`). Double-click it to run — one native window, no browser, no console.

The shell spawns the frozen server with `--auto-shutdown`, so the server stops itself 30 seconds after the last UI disconnects even if the window process dies; closing the window normally tree-kills the server immediately. User data (scripts, workspaces, scenarios, designs) persists in `%LOCALAPPDATA%\ScriptPlayground\`; the seeded `demo_bot` sample and bundled resources are read-only.

### Native notifications and tray

The shell adds two OS integrations:

- **Tray icon** (Windows) — *Show ScriptPlayground*, a **Sound effects** checkbox wired to the in-app 🔊 toggle (both stay in sync; the web client never echoes tray-originated changes back), and *Quit*. Left-clicking the tray icon focuses the window.
- **Native notifications** — when messages arrive while the window is hidden and sounds are on, the page asks the shell for a real Windows toast (`Author · #channel`). Clicking it focuses the window.

Windows toast prerequisites the shell self-heals on launch: an App User Model ID (`com.ean.scriptplayground`), a Start Menu shortcut carrying it, and a per-user registry entry. If toasts are disabled in Windows Settings (system-wide or via Do Not Disturb), `Notification.show()` fails with `WPN_E_NOTIFICATION_TYPE_DISABLED` (`0x803E0114`) and the shell logs it — the in-app sound cues are unaffected.

End-to-end verification (what a release check should cover): launch the exe, confirm `GET /` serves the UI, confirm the `demo_bot` workspace is discovered from the user-data root, run it (project boot → `ready=true`), invoke its `/hello` slash command and see the bot reply in the timeline, then close the window and confirm no `ScriptPlayground*` processes and no listening ports remain.

`node scripts/walk_desktop.js --packaged` automates that release check: it launches the built exe via Playwright, connects `showcase_bot`, runs it, drives `/panel` + the Wave button, adds a 🔥 reaction from the hover toolbar, opens the user-settings overlay (appearance + sound panes, switch toggle, Escape), sweeps for browser windows before/during/after, and verifies clean shutdown. Plain `node scripts/walk_desktop.js` walks the dev checkout the same way.

## Legacy PyInstaller-only build (browser/Chrome-app mode)

On Windows, from the project root run:

```powershell
.\scripts\build_windows.ps1
.\scripts\verify_exe.ps1
```

The first command installs PyInstaller into the active Python environment only if it is missing (PyInstaller is a build-time tool and is not in `requirements.txt`), builds a one-folder bundle, and reports the executable size. The release artifact is `dist\ScriptPlayground\ScriptPlayground.exe`; the verification script starts that exact exe and exercises its HTTP routes, real demo interactions, bundled resources, user-data seeding, parity with Python mode, and graceful Ctrl+Break shutdown. The bundle avoids one-file self-extraction on every launch. Keep build output separate from runtime user data.

Reproducing the verification by hand: launch `dist\ScriptPlayground\ScriptPlayground.exe --port 8812` (set `SCRIPTPLAYGROUND_NO_BROWSER=1` to skip the browser — the exe does not accept `--no-browser`), then `GET /`, `POST /api/session`, `POST /api/session/{sid}/run` with the demo script, interact, and stop with Ctrl+Break; a clean shutdown logs `ScriptPlayground stopped`. The bundle also ships `node_shim/` under `_internal\`, so Node/discord.js workspaces run from the frozen app (requires `node` on PATH).

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
