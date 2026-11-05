"""Build the installer for the platform this runs on, and its in-app update.

    macOS    dist/Simplixity-<version>-macos-<arch>.dmg
    Windows  dist/Simplixity-<version>-windows-x64-setup.exe   (needs Inno Setup 6)
    Linux    dist/Simplixity-<version>-linux-x86_64.AppImage   (downloads appimagetool)
    all      dist/Simplixity-payload-<platform>.zip + .json    (see updater.py)

PyInstaller cannot cross-compile, so each one is built on its own OS; the
GitHub workflow (.github/workflows/desktop.yml) runs this on all three.

    uv sync --group build
    uv run python fetch.py     # Simplixity and SearXNG at the refs in upstream.toml
    uv run python build.py
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE / ".simplixity"
DIST = HERE / "dist"
WORK = HERE / "build"
ICON_PNG = REPO / "web" / "assets" / "icons" / "icon-512.png"
NAME = "Simplixity"

if not (REPO / "pyproject.toml").is_file():
    sys.exit("Simplixity source missing; run `python fetch.py` first.")
# The app carries the version of the Simplixity it is built from. In a release
# build that is the tag (desktop-v0.2.1, or desktop-v0.2.1-2 for a second
# release on it), named after Simplixity's release tag: upstream does not
# always bump its pyproject.toml. Otherwise pyproject.toml's.
_tag = re.fullmatch(r"desktop-v(\d+\.\d+\.\d+)(?:-\d+)?", os.environ.get("GITHUB_REF_NAME", ""))
VERSION = _tag[1] if _tag else tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def run(*cmd: str | Path, **kw) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


# --- icons -----------------------------------------------------------------


def icon_for_platform() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        iconset = WORK / "Simplixity.iconset"
        shutil.rmtree(iconset, ignore_errors=True)
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                if px > 512:
                    continue
                suffix = "@2x" if scale == 2 else ""
                run("sips", "-z", px, px, ICON_PNG, "--out", iconset / f"icon_{size}x{size}{suffix}.png",
                    stdout=subprocess.DEVNULL)
        icns = WORK / "Simplixity.icns"
        run("iconutil", "-c", "icns", iconset, "-o", icns)
        return icns
    if sys.platform == "win32":
        from PIL import Image

        ico = WORK / "Simplixity.ico"
        Image.open(ICON_PNG).save(ico, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
        return ico
    return ICON_PNG


# --- PyInstaller -----------------------------------------------------------


def pyinstaller(icon: Path) -> None:
    if not (HERE / ".searxng" / "searx").is_dir():
        sys.exit("SearXNG source missing; run `python fetch.py` first.")
    # What updater.py compares an update against (see release_info).
    shell_json = WORK / "shell.json"
    shell_json.write_text(json.dumps(release_info()), encoding="utf-8")
    env = {**os.environ, "SIMPLIXITY_ICON": str(icon), "SIMPLIXITY_VERSION": VERSION,
           "SIMPLIXITY_SHELL_JSON": str(shell_json)}
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", DIST / "pyinstaller", "--workpath", WORK / "pyinstaller",
        HERE / "simplixity.spec", env=env)


# --- in-app update (updater.py) ---------------------------------------------


def release_info() -> dict:
    """shell: what an update must have been built for; serial: orders releases
    (the commit time, the same in every job of one release); version: shown."""
    import updater

    serial = int(subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=HERE, check=True,
                                capture_output=True, text=True).stdout.strip())
    tag = os.environ.get("GITHUB_REF_NAME", "")
    version = tag.removeprefix("desktop-") if tag.startswith("desktop-v") else f"{VERSION}+{serial}"
    return {"shell": updater.fingerprint(HERE), "serial": serial, "version": version}


# The update carries everything but the shell: the code, and the packages it
# needs (lib/, at the versions in uv.lock). Only the shell's own packages stay out.
SHELL_ONLY_PACKAGES = {"pywebview"}
PAYLOAD_TREES = [
    (REPO / "app", "app"),
    (REPO / "web", "web"),
    (REPO / "docker-compose.yml", "docker-compose.yml"),
    (HERE / ".searxng" / "searx", "searx"),
    (HERE / "skin", "skin"),
    (HERE / "desktop_api.py", "desktop_api.py"),
    (HERE / "skin.py", "skin.py"),
]


def package_payload() -> list[Path]:
    import updater

    uv = shutil.which("uv") or sys.exit("uv is needed to build the update (https://docs.astral.sh/uv/).")
    WORK.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    lib = WORK / "payload-lib"
    shutil.rmtree(lib, ignore_errors=True)
    deps = tomllib.loads((HERE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    wanted = [d for d in deps if re.split(r"[\s<>=!~;\[]", d, maxsplit=1)[0].lower() not in SHELL_ONLY_PACKAGES]
    (WORK / "payload-requirements.txt").write_text("\n".join(wanted) + "\n", encoding="utf-8")
    run(uv, "export", "--locked", "--no-hashes", "--no-header", "--no-annotate", "--no-emit-project",
        "--format", "requirements-txt", "-o", WORK / "payload-constraints.txt", cwd=HERE)
    run(uv, "pip", "install", "--quiet", "--python", sys.executable, "--target", lib,
        "-r", WORK / "payload-requirements.txt", "-c", WORK / "payload-constraints.txt")
    shutil.rmtree(lib / "bin", ignore_errors=True)

    name = updater.payload_name()
    zip_path, manifest = DIST / f"{name}.zip", DIST / f"{name}.json"
    entries: list[tuple[Path, str]] = []
    for src, dest in [*PAYLOAD_TREES, (lib, "lib")]:
        if src.is_file():
            entries.append((src, dest))
            continue
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if path.is_file() and not path.is_symlink() and "__pycache__" not in rel.parts and path.suffix != ".pyc":
                entries.append((path, f"{dest}/{rel.as_posix()}"))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, dest in entries:
            info = zipfile.ZipInfo(dest, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, src.read_bytes())
    manifest.write_text(json.dumps(release_info()), encoding="utf-8")
    return [zip_path, manifest]


# --- packaging -------------------------------------------------------------


def package_macos() -> Path:
    arch = platform.machine()  # arm64 or x86_64
    staging = WORK / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    shutil.copytree(DIST / "pyinstaller" / f"{NAME}.app", staging / f"{NAME}.app", symlinks=True)
    (staging / "Applications").symlink_to("/Applications")
    out = DIST / f"{NAME}-{VERSION}-macos-{arch}.dmg"
    out.unlink(missing_ok=True)
    run("hdiutil", "create", "-volname", NAME, "-srcfolder", staging, "-ov", "-format", "UDZO", out)
    return out


ISS = r"""
[Setup]
AppId={{6D65F9E6-560D-4931-A0D7-23B1DFF398F4}
AppName=Simplixity
AppVersion=%(version)s
AppPublisher=Simplixity
DefaultDirName={autopf}\Simplixity
DefaultGroupName=Simplixity
PrivilegesRequired=lowest
OutputDir=%(outdir)s
OutputBaseFilename=%(basename)s
SetupIconFile=%(icon)s
UninstallDisplayIcon={app}\Simplixity.exe
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Files]
Source: "%(src)s\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Simplixity"; Filename: "{app}\Simplixity.exe"
Name: "{autodesktop}\Simplixity"; Filename: "{app}\Simplixity.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\Simplixity.exe"; Description: "Start Simplixity"; Flags: nowait postinstall skipifsilent
"""


def package_windows(icon: Path) -> Path:
    iscc = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if not Path(iscc).exists():
        sys.exit("Inno Setup 6 not found (choco install innosetup).")
    basename = f"{NAME}-{VERSION}-windows-x64-setup"
    iss = WORK / "simplixity.iss"
    iss.write_text(ISS % {
        "version": VERSION,
        "outdir": DIST,
        "basename": basename,
        "icon": icon,
        "src": DIST / "pyinstaller" / NAME,
    }, encoding="utf-8")
    run(iscc, iss)
    return DIST / f"{basename}.exe"


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Simplixity
Comment=Research assistant that answers with checked citations
Exec=Simplixity
Icon=simplixity
Categories=Network;Utility;
Terminal=false
"""

APPRUN = """#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/simplixity/Simplixity" "$@"
"""


# appimagetool at a fixed release, checked against its SHA-256 (GitHub's asset
# digest): it runs in the release build, so a swapped download would end up in
# every AppImage.
APPIMAGETOOL_VERSION = "1.9.1"
APPIMAGETOOL_SHA256 = {
    "x86_64": "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0",
    "aarch64": "f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158",
}


def appimagetool(arch: str) -> Path:
    if os.environ.get("APPIMAGETOOL"):
        return Path(os.environ["APPIMAGETOOL"])
    if arch not in APPIMAGETOOL_SHA256:
        sys.exit(f"No appimagetool for {arch}; set APPIMAGETOOL to one.")
    tool = WORK / f"appimagetool-{APPIMAGETOOL_VERSION}-{arch}.AppImage"
    if not tool.exists():
        url = (f"https://github.com/AppImage/appimagetool/releases/download/"
               f"{APPIMAGETOOL_VERSION}/appimagetool-{arch}.AppImage")
        print("+ download", url, flush=True)
        part = tool.with_suffix(".part")
        urllib.request.urlretrieve(url, part)
        digest = hashlib.sha256(part.read_bytes()).hexdigest()
        if digest != APPIMAGETOOL_SHA256[arch]:
            part.unlink()
            sys.exit(f"appimagetool download does not match its SHA-256 ({digest}).")
        part.rename(tool)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return tool


def package_linux() -> Path:
    arch = platform.machine()  # x86_64 or aarch64
    appdir = WORK / "Simplixity.AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(DIST / "pyinstaller" / NAME, appdir / "usr" / "lib" / "simplixity", symlinks=True)
    (appdir / "simplixity.desktop").write_text(DESKTOP_ENTRY, encoding="utf-8")
    shutil.copy(ICON_PNG, appdir / "simplixity.png")
    apprun = appdir / "AppRun"
    apprun.write_text(APPRUN, encoding="utf-8")
    # 0755: an AppImage is run by other users than the one who built it (appimagetool checks).
    apprun.chmod(0o755)

    tool = appimagetool(arch)

    out = DIST / f"{NAME}-{VERSION}-linux-{arch}.AppImage"
    out.unlink(missing_ok=True)
    # --appimage-extract-and-run: CI runners have no FUSE for the tool itself.
    run(tool, "--appimage-extract-and-run", appdir, out, env={**os.environ, "ARCH": arch})
    return out


def main() -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    icon = icon_for_platform()
    pyinstaller(icon)
    if sys.platform == "darwin":
        out = package_macos()
    elif sys.platform == "win32":
        out = package_windows(icon)
    else:
        out = package_linux()
    print()
    for f in [out, *package_payload()]:
        print(f"built {f} ({f.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
