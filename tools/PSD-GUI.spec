from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


root = Path(SPECPATH).parent
icon = root / "build" / "macos" / "PSD-GUI.icns"
datas = collect_data_files("psid_graph_viewer")
datas += collect_data_files("power_system_component_icons")

analysis = Analysis(
    [str(root / "tools" / "macos_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("NodeGraphQt")
    + [
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtNetwork",
        "PySide6.QtSvg",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PSD-GUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch="arm64",
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="PSD-GUI",
)
app = BUNDLE(
    collection,
    name="PSD-GUI.app",
    icon=str(icon),
    bundle_identifier="org.powersimulationsdynamics.psd-gui",
    info_plist={
        "CFBundleDisplayName": "PSD-GUI",
        "CFBundleName": "PSD-GUI",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    },
)
