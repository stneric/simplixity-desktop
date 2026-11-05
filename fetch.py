"""Check out the pinned Simplixity and SearXNG sources the desktop app is built from.

    python fetch.py            # both, at the refs in upstream.toml
    python fetch.py --check    # only verify that our dependencies cover upstream's

Sources land in .simplixity/ and .searxng/ (git-ignored). Neither is copied
into this repo: the desktop app is a shell around an unmodified Simplixity,
and the pin in upstream.toml decides which one.

SIMPLIXITY_SRC=/path/to/simplixity points the launcher at a local checkout
instead (for trying a branch); fetch.py then only runs the dependency check
against it.

Stdlib only, so it runs before `uv sync`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINS = tomllib.loads((HERE / "upstream.toml").read_text(encoding="utf-8"))
TARGETS = {"simplixity": HERE / ".simplixity", "searxng": HERE / ".searxng"}
# Only what the app uses is checked out. For SearXNG that is searx/: the rest
# of its tree (utils/, docs, container files) is never read, and holds file
# names Windows cannot have (utils/templates/.../searxng.conf:socket).
SPARSE = {"searxng": ["searx"]}


def git(*args: str, cwd: Path) -> str:
    # core.longpaths: SearXNG's tree has paths past Windows' 260-character limit.
    r = subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed in {cwd}:\n{r.stderr.strip()}")
    return r.stdout.strip()


def checkout(name: str) -> None:
    repo, ref = PINS[name]["repo"], PINS[name]["ref"]
    dest = TARGETS[name]
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        git("init", "-q", cwd=dest)
        git("remote", "add", "origin", repo, cwd=dest)
    else:
        git("remote", "set-url", "origin", repo, cwd=dest)
    # A shallow fetch of exactly one ref works for tags, branches and full SHAs
    # alike on GitHub, and skips the history nobody here reads.
    print(f"{name}: fetching {ref} from {repo}", flush=True)
    git("fetch", "-q", "--depth", "1", "--tags", "origin", ref, cwd=dest)
    if name in SPARSE:
        # Git for Windows refuses such a name already in the index, before the
        # sparse pattern keeps it off the disk; with core.protectNTFS off it
        # takes the entry, and the file outside searx/ is still never written.
        # Only for this checkout of a pinned commit, not for git in general.
        git("config", "core.protectNTFS", "false", cwd=dest)
        git("sparse-checkout", "set", *SPARSE[name], cwd=dest)
    git("checkout", "-q", "--force", "--detach", "FETCH_HEAD", cwd=dest)
    git("clean", "-qfdx", "-e", "__pycache__", cwd=dest)
    print(f"{name}: at {git('log', '-1', '--format=%h (%cd)', cwd=dest)}", flush=True)


def simplixity_src() -> Path:
    return Path(os.environ["SIMPLIXITY_SRC"]).resolve() if os.environ.get("SIMPLIXITY_SRC") else TARGETS["simplixity"]


def _names(deps: list[str]) -> set[str]:
    return {re.split(r"[\s<>=!~;\[@]", d, maxsplit=1)[0].lower().replace("_", "-") for d in deps}


def check_dependencies() -> None:
    """Fail loudly when upstream needs a package this app's pyproject lacks.

    Otherwise the bundle builds fine and the window shows an ImportError at
    runtime, which is a worse place to learn it.
    """
    upstream = tomllib.loads((simplixity_src() / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    ours = tomllib.loads((HERE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    missing = sorted(_names(upstream) - _names(ours))
    if missing:
        sys.exit(
            "Simplixity depends on packages missing from pyproject.toml here:\n  "
            + "\n  ".join(missing)
            + "\nAdd them (then `uv lock`), or keep the previous ref in upstream.toml."
        )
    print("dependencies: covered", flush=True)


def main() -> None:
    if "--check" not in sys.argv:
        if not os.environ.get("SIMPLIXITY_SRC"):
            checkout("simplixity")
        checkout("searxng")
    check_dependencies()


if __name__ == "__main__":
    main()
