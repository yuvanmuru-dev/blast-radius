"""Gate harness: run the selector on a dataset and report exact cost, misses, CPU.

usage: python evaluate.py DATASET_DIR ANSWERS_JSON [--sqlglot DIR] [--map MAP] [-v]
DATASET_DIR holds tests.json and commits/. ANSWERS is {"commits": {id: {"failing": [...]}}}
or a flat {id: [...]} label file. Exit code 1 on any miss or invalid output.
"""
import argparse, json, os, resource, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("data"); ap.add_argument("answers")
ap.add_argument("--sqlglot", default=str(HERE.parent.parent / "sqlglot"))
ap.add_argument("--map", default=None)
ap.add_argument("-v", action="store_true")
a = ap.parse_args()

ans = json.load(open(a.answers))
ans = {k: (v["failing"] if isinstance(v, dict) else v) for k, v in ans.get("commits", ans).items()}
tests = json.load(open(Path(a.data) / "tests.json"))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
commits = sorted(p.stem for p in (Path(a.data) / "commits").glob("*.patch"))

env = dict(os.environ, BR_DATA=a.data, BR_SQLGLOT=a.sqlglot)
if a.map:
    env["BR_MAP"] = a.map
r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
t0 = time.time()
p = subprocess.run([sys.executable, str(HERE.parent / "selector" / "main.py")], env=env, capture_output=True, text=True)
wall = time.time() - t0
r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
if p.returncode != 0:
    print(p.stderr[-3000:]); sys.exit(1)

seen, total_ms, misses, bad = set(), 0, [], []
lines = p.stdout.split("\n")
if lines and lines[-1] == "":
    lines.pop()
rows = []
for i, ln in enumerate(lines, 1):
    d = json.loads(ln)
    assert set(d) == {"commit", "workers"}, f"shape line {i}"
    c = d["commit"]; assert c in commits and c not in seen, f"commit line {i}"
    seen.add(c)
    ws = [w for w in d["workers"] if w]
    flat = [x for w in ws for x in w]
    assert all(isinstance(x, int) and x in dur for x in flat), f"test line {i}"
    assert len(flat) == len(set(flat)), f"repeat line {i}"
    assert len(ws) <= 8, f"workers line {i}"
    cost = (max(sum(dur[x] for x in w) for w in ws) + 2000 * len(ws)) if ws else 0
    fail = set(ans.get(c, []))
    missed = fail - set(flat)
    if c not in ans:
        bad.append(c)
    if missed:
        misses.append((c, sorted(missed)))
    # oracle: exact failing set, best packing
    sys.path.insert(0, str(HERE.parent / "selector"))
    import main as sel  # noqa
    ow = sel.pack(sorted(fail), dur)
    ocost = (max(sum(dur[x] for x in w) for w in ow) + 2000 * len(ow)) if ow else 0
    rows.append((c, cost, ocost, len(flat), len(fail), bool(missed)))
    total_ms += cost
assert seen == set(commits), "missing commit"
oracle = sum(r[2] for r in rows)
sim = total_ms / 1000 + 300 * len(misses)
print(f"commits {len(commits)}  simulated {sim:.3f}s  oracle {oracle/1000:.3f}s  excess {sim-oracle/1000:.3f}s  "
      f"misses {len(misses)}  cpu {cpu:.2f}s  wall {wall:.2f}s  unlabeled {len(bad)}")
if a.v:
    for c, cost, oc, n, nf, m in sorted(rows, key=lambda r: r[2] - r[1]):
        if cost != oc or m:
            print(f"  {c}: cost {cost/1000:.3f} oracle {oc/1000:.3f} selected {n} failing {nf}{'  MISS' if m else ''}")
for c, m in misses:
    print("  MISS", c, m)
sys.exit(1 if misses else 0)
