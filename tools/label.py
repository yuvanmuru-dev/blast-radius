"""Ground-truth labeler: apply a patch to a copy of sqlglot and run the suite.

usage: python label.py SQLGLOT_DIR TESTS_JSON PATCH [PATCH...] --out labels.json [-n 2] [--only ids.txt]
Prints {commit: [failing test numbers]}.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "selector"))
import difflib2  # noqa

COPY = ["sqlglot", "tests", "setup.py", "setup.cfg", "pyproject.toml", "sqlglotc", "MANIFEST.in", "README.md"]


def make_tree(src, patch_text, dst):
    for name in COPY:
        s = Path(src) / name
        if s.is_dir():
            shutil.copytree(s, Path(dst) / name, ignore=shutil.ignore_patterns("__pycache__"))
        elif s.exists():
            shutil.copy2(s, Path(dst) / name)
    for fp in difflib2.parse(patch_text):
        if fp.binary:
            raise ValueError("binary")
        old_p = Path(dst) / fp.old if fp.old else None
        old = old_p.read_text() if old_p and old_p.exists() else None
        new = difflib2.apply(old, fp)
        if fp.old and fp.new != fp.old and old_p.exists():
            old_p.unlink()
        if fp.new:
            np_ = Path(dst) / fp.new
            np_.parent.mkdir(parents=True, exist_ok=True)
            np_.write_text(new)


def run(tree, n, ids=None, timeout=None):
    junit = Path(tree) / "junit.xml"
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE="1")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", "-o", "junit_family=xunit1"]
    if n and n > 1:
        cmd += ["-n", str(n)]
    if timeout:
        cmd += [f"--timeout={timeout}"]
    cmd += ids if ids else ["tests"]
    r = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True)
    return junit, r


def failing_ids(junit, r):
    fails, collect_err = set(), set()
    if not junit.exists():
        raise RuntimeError("no junit: " + r.stdout[-3000:] + r.stderr[-3000:])
    root = ET.parse(junit).getroot()
    for tc in root.iter("testcase"):
        bad = any(ch.tag in ("failure", "error") for ch in tc)
        if not bad:
            continue
        cls, name, f = tc.get("classname", ""), tc.get("name", ""), tc.get("file")
        if not name or name in ("", None) or cls.endswith("tests") and not f:
            collect_err.add(cls)
        parts = cls.split(".")
        # classname like tests.test_parser.TestParser -> tests/test_parser.py::TestParser::name
        for i in range(len(parts), 0, -1):
            p = "/".join(parts[:i]) + ".py"
            if (junit.parent / p).exists():
                rest = parts[i:]
                tid = "::".join([p] + rest + ([name] if name else []))
                fails.add(tid)
                if not rest and not name or name == "" :
                    collect_err.add(p)
                break
        else:
            collect_err.add(cls)
    return fails, collect_err, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlglot"); ap.add_argument("tests_json"); ap.add_argument("patches", nargs="+")
    ap.add_argument("--out", required=True); ap.add_argument("-n", type=int, default=2)
    ap.add_argument("--scope-map", default=None, help="map.json.gz: only run tests that touch the changed files")
    a = ap.parse_args()
    M = None
    if a.scope_map:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "selector"))
        import rules
        tl = json.load(open(a.tests_json))["tests"]
        M = rules.Map(a.scope_map, len(tl), tl)
        id_of = {t["number"]: t["id"] for t in tl}
    tests = json.load(open(a.tests_json))["tests"] if a.tests_json != "-" else []
    by_id = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    out = json.load(open(a.out)) if os.path.exists(a.out) else {}
    for p in a.patches:
        cid = Path(p).stem
        if cid in out:
            continue
        with tempfile.TemporaryDirectory() as d:
            try:
                make_tree(a.sqlglot, Path(p).read_text(), d)
            except Exception as e:
                print(cid, "APPLY FAIL", e, file=sys.stderr); continue
            ids = None
            if M is not None:
                bits = M.always
                for fp in difflib2.parse(Path(p).read_text()):
                    for f in (fp.old, fp.new):
                        if f:
                            bits |= M.file_any(f) | M.file_tests(f)
                ids = [id_of[n] for n in sorted(rules.bits_to_set(bits))]
                if not ids:
                    out[cid] = []
                    print(cid, "0 (no test touches it)", file=sys.stderr, flush=True)
                    json.dump(out, open(a.out, "w"))
                    continue
                if len(ids) > 1500:
                    ids = None
            junit, r = run(d, a.n, ids=ids)
            fails, cerr, r = failing_ids(junit, r)
            nums, unknown = set(), []
            for f in fails:
                if f in by_id:
                    nums.add(by_id[f])
                else:
                    # collection error on a file, or a parametrized/odd id
                    hit = [t for t in by_file if f.startswith(t)]
                    if hit:
                        for h in hit:
                            if f == h or f.startswith(h + "::") and f.count("::") < 2:
                                nums.update(by_file[h])
                    unknown.append(f)
            out[cid] = sorted(nums) if tests else sorted(fails)
            print(cid, len(nums), "unknown:", unknown[:5], file=sys.stderr, flush=True)
            json.dump(out, open(a.out, "w"))


if __name__ == "__main__":
    main()
