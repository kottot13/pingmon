# PyInstaller spec — build a single self-contained `pingmon` binary
# (no Python required on the target machine), the most htop-like option.
#
# Build:
#   pip install pyinstaller
#   pyinstaller packaging/pingmon.spec
# Result:
#   dist/pingmon        <- copy to /usr/local/bin (macOS) or ~/.local/bin (Linux)
#
# Notes:
#   * --collect-all textual bundles Textual's own data files.
#   * app.tcss is added under the `pingmon` package dir so Textual's CSS_PATH
#     (resolved relative to the app module) finds it inside the bundle.
#   * Build on each target OS/arch separately (macOS arm64, macOS x86_64,
#     Linux x86_64); PyInstaller does not cross-compile.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("textual")
datas += [("../pingmon/app.tcss", "pingmon")]

a = Analysis(
    ["../pingmon/__main__.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pingmon",
    console=True,        # it is a terminal app
    onefile=True,
    strip=False,
    upx=True,
)
