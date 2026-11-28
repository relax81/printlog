"""Versionsanzeige: Version aus dem letzten Commit, Fußzeile und /api/version.

Aufruf: python -m pytest tests
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import version  # noqa: E402
from app.ui import layout  # noqa: E402


def _repo(tmp: Path, subject: str) -> Path:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(tmp)]
    subprocess.run(["git", "init", "-q", str(tmp)], check=True)
    (tmp / "a.txt").write_text("x")
    subprocess.run(git + ["add", "a.txt"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", subject], check=True)
    return tmp


def test_pr_nummer_aus_merge_commit(tmp_path: Path) -> None:
    v = version.build_info(_repo(tmp_path, "Versionsanzeige einbauen (#53)"))
    assert v["number"] == 53 and len(v["commit"]) >= 7 and v["built"] and v["date"]
    assert version.label(v) == "v53"


def test_ohne_pr_nummer_hash(tmp_path: Path) -> None:
    v = version.build_info(_repo(tmp_path, "Direkter Commit"))
    assert v["number"] is None and version.label(v) == v["commit"]


def test_ohne_git(tmp_path: Path) -> None:
    v = version.build_info(tmp_path)
    assert v["commit"] is None and v["built"] and version.label(v) == "dev"


def test_fusszeile(monkeypatch) -> None:
    monkeypatch.setattr(version, "_CACHE", {"number": 53, "commit": "abc1234", "date": 1, "subject": "X (#53)",
                                            "built": 1764300000})
    html = layout("Test", "jobs", "<p>x</p>")
    assert '<footer class="ver">' in html and "printlog v53" in html and "abc1234" in html and "Build " in html
    assert "/commit/abc1234" in html


def test_datei_schreiben(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", "Test (#7)")
    out = tmp_path / "v.json"
    subprocess.run([sys.executable, "-m", "app.version", "--write", str(out)], cwd=repo, check=True,
                   env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent), "PATH": "/usr/bin:/bin"})
    assert json.loads(out.read_text())["number"] == 7
