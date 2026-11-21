"""In-container verification: run candidate tests against the patched tree.

The selector builds a symlink overlay of the pinned checkout in /tmp with the
patched files written for real, then runs `runner.py` there. The runner imports
the needed test modules once, then forks one child per test class and runs the
selected methods (setUpClass included), reporting which tests failed. Anything
odd (crash, timeout, import error, unparsed output) counts as failing, so
verification can only ever drop a test that really passed.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build_overlay(src, root, files):
    """files: {relpath: new text or None (deleted)}. Returns overlay root."""
    src = Path(src)
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    # the set of directories along patched paths must be real
    real_dirs = {Path("")}
    for rel in files:
        p = Path(rel).parent
        while p != Path("") and p != Path("."):
            real_dirs.add(p)
            p = p.parent

    def fill(rel_dir):
        sdir = src / rel_dir
        ddir = root / rel_dir
        ddir.mkdir(exist_ok=True)
        for entry in os.listdir(sdir):
            rel = (rel_dir / entry) if rel_dir != Path("") else Path(entry)
            rs = str(rel)
            if rs in files:
                continue
            if rel in real_dirs:
                fill(rel)
            else:
                os.symlink(sdir / entry, ddir / entry)

    fill(Path(""))
    for rel, text in files.items():
        if text is None:
            continue
        d = root / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(text, encoding="utf-8")
    return root


def run(overlay, tests, timeout):
    """tests: list of (number, id, seconds). Returns set of numbers that FAILED
    (or could not be confirmed as passing)."""
    req = json.dumps([[n, i, s] for n, i, s in tests])
    env = dict(os.environ)
    env.update(SKIP_INTEGRATION="0", PYTHONPATH=str(overlay), PYTHONDONTWRITEBYTECODE="1",
               HOME=env.get("HOME", "/tmp"))
    nums = {n for n, _, _ in tests}
    try:
        p = subprocess.run(
            [sys.executable, str(HERE / "runner.py")],
            input=req, capture_output=True, text=True, cwd=str(overlay), env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return nums
    passed = set()
    for ln in p.stdout.splitlines():
        if ln.startswith("BRPASS "):
            try:
                passed.add(int(ln.split()[1]))
            except ValueError:
                pass
    return nums - passed
