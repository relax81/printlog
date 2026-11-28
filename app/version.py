"""Versionsanzeige: Welcher Stand läuft gerade?

Die Version wird nicht von Hand gepflegt. Beim Docker-Build liest ``python -m
app.version --write <datei>`` den letzten Commit aus ``.git`` (Portainer klont das
Repo, dabei ist ``.git`` vorhanden) und schreibt ``app/version.json``:

* ``number``: PR-Nummer aus dem Merge-Commit („… (#52)“), steigt mit jedem Merge
* ``commit``: kurzer Commit-Hash, ``date``: Commit-Zeit, ``subject``: Commit-Titel
* ``built``: Zeitpunkt des Builds (immer gesetzt, auch ohne ``.git``)

Ohne Datei (lokal gestartet) wird ``.git`` direkt gelesen, sonst gilt „dev“.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FILE = Path(__file__).resolve().parent / "version.json"
REPO_URL = "https://github.com/relax81/printlog"


def from_git(root: Path = ROOT) -> dict[str, Any]:
    """Letzter Commit aus ``.git``; leer, wenn es kein Repo oder kein git gibt."""
    if not (root / ".git").exists():
        return {}
    try:
        out = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "log", "-1", "--format=%h%n%ct%n%s"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return {}
    if len(out) < 3:
        return {}
    commit, ts, subject = out[0].strip(), out[1].strip(), out[2].strip()
    m = re.search(r"\(#(\d+)\)\s*$", subject)
    return {"number": int(m.group(1)) if m else None, "commit": commit,
            "date": int(ts) if ts.isdigit() else None, "subject": subject}


def build_info(root: Path = ROOT) -> dict[str, Any]:
    return {"number": None, "commit": None, "date": None, "subject": None,
            **from_git(root), "built": int(time.time())}


_CACHE: dict[str, Any] | None = None


def info() -> dict[str, Any]:
    """Version des laufenden Stands (einmal gelesen und gemerkt)."""
    global _CACHE
    if _CACHE is None:
        data: dict[str, Any] = {}
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {**from_git(), "built": None}
        _CACHE = {"number": None, "commit": None, "date": None, "subject": None, "built": None, **data}
    return _CACHE


def label(v: dict[str, Any] | None = None) -> str:
    """Kurzform für die Anzeige: „v52“, sonst Commit-Hash, sonst „dev“."""
    v = info() if v is None else v
    if v.get("number"):
        return f"v{v['number']}"
    if v.get("commit"):
        return str(v["commit"])
    return "dev"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write":
        root = Path.cwd()
        data = build_info(root)
        Path(sys.argv[2]).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print("printlog version:", label(data), data.get("commit") or "(kein .git)")
    else:
        print(json.dumps(info(), ensure_ascii=False))
