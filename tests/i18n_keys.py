"""Sammelt alle übersetzbaren Texte aus app/ (t, tn, N_ und _t("…") in JavaScript)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
JS_RE = re.compile(r'_t\("((?:[^"\\]|\\.)*)"\)')


def keys() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(APP.glob("*.py")):
        if path.name == "i18n_en.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                name = node.func.id
                args = node.args[1:] if name == "tn" else node.args[:1] if name in ("t", "N_") else []
                for a in args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        found.setdefault(a.value, path.name)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in JS_RE.finditer(node.value):
                    found.setdefault(m.group(1), path.name)
    return found


if __name__ == "__main__":
    for k, f in keys().items():
        print(f"{f}\t{k}")
