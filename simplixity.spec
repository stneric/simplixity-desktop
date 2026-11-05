# PyInstaller spec for Simplixity Desktop. Built by build.py; run it from there.
#
# Layout inside the bundle (sys._MEIPASS):
#   app/, searx/            Python packages, compiled into the archive as usual
#   searx/**  (sources)     SearXNG's tree once more as plain files: it loads its
#                           engines with spec_from_file_location from .py files on
#                           disk, and reads templates, translations and data there
#   web/                    Simplixity's frontend (app/main.py resolves ../web)
#   skin/                   the desktop skin, see skin.py
#   docker-compose.yml      source of SearXNG's settings.yml, see the launcher
#   shell.json              what updates must match, see updater.py
#   the standard library    all of it: an update's code and packages may need a
#                           module the built-in code never imported
#
# app/, web/ and docker-compose.yml come from .simplixity (fetch.py), at the
# ref pinned in upstream.toml.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = Path(SPECPATH)
REPO = HERE / ".simplixity"
SXNG = HERE / ".searxng"
sys.path.insert(0, str(SXNG))
sys.path.insert(0, str(REPO))

ICON = os.environ.get("SIMPLIXITY_ICON")  # set by build.py per platform


def tree(src: Path, dest: str) -> list[tuple[str, str]]:
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = Path(root).relative_to(src)
        for f in files:
            out.append((str(Path(root) / f), str(Path(dest) / rel)))
    return out


datas = [
    *tree(REPO / "web", "web"),
    *tree(SXNG / "searx", "searx"),
    *tree(HERE / "skin", "skin"),
    (str(REPO / "docker-compose.yml"), "."),
    # Written by build.py: the shell fingerprint and build serial, see updater.py.
    (os.environ["SIMPLIXITY_SHELL_JSON"], "."),
]
binaries = []
hiddenimports = [
    *collect_submodules("app"),
    # SearXNG's modules, engines included, so their imports end up in the bundle
    # even though the engines themselves are loaded from the source files above.
    *collect_submodules("searx", filter=lambda name: name != "searx.webapp"),
    "searx.webapp",
]

# The whole standard library, minus what a window app never uses.
STDLIB_SKIP = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip",
               "venv", "pydoc_data", "this", "antigravity", "__phello__"}
for name in sorted(sys.stdlib_module_names):
    if name.startswith("_") or name in STDLIB_SKIP:
        continue
    try:
        hiddenimports += collect_submodules(name, filter=lambda m: ".test" not in m)
    except Exception:  # noqa: BLE001 - another platform's module (winreg, msvcrt, ...)
        pass

# Packages that load modules or data by name at runtime.
for pkg in ("pygments", "babel", "flask_babel", "curl_cffi", "lxml", "certifi", "fakeredis", "msgspec"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(HERE / "simplixity_desktop.py")],
    pathex=[str(REPO), str(SXNG)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # macos.py is imported on macOS only, and needs AppKit to even be analysed.
    excludes=["tkinter", "pytest", "IPython", *([] if sys.platform == "darwin" else ["macos"])],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    # UTF-8 mode: files are read as UTF-8 without asking, also in Simplixity's
    # and SearXNG's code. Windows would otherwise use its ANSI code page.
    [("X utf8_mode=1", None, "OPTION")],
    exclude_binaries=True,
    name="Simplixity",
    console=False,
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Simplixity")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Simplixity.app",
        icon=ICON,
        bundle_identifier="io.github.stneric.simplixity-desktop",
        info_plist={
            "CFBundleShortVersionString": os.environ.get("SIMPLIXITY_VERSION", "0.0.0"),
            "NSHighResolutionCapable": True,
            # The window talks to its own server on 127.0.0.1 over plain HTTP.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
