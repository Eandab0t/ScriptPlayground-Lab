# PyInstaller one-folder build for the ScriptPlayground server consumed by the
# Electron desktop shell (release/ extraResources -> server/ScriptPlayground-server.exe).
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "static"), "static"),
        (str(root / "embeder"), "embeder"),
        (str(root / "scripts"), "scripts"),
        (str(root / "assets"), "assets"),
        (str(root / "node_shim"), "node_shim"),
    ],
    hiddenimports=["bot_runtime", "bridge", "env_discovery", "project_state"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScriptPlayground-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "icon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="server",
)
