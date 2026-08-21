from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


backend_root = Path(SPECPATH).resolve()

analysis = Analysis(
    [str(backend_root / "main.py")],
    pathex=[str(backend_root)],
    binaries=[],
    datas=[
        (str(backend_root / "settings" / "static"), "settings/static"),
    ],
    hiddenimports=collect_submodules("keyring.backends"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=0,
)

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="vaa-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
