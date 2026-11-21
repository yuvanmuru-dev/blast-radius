"""Compile map_raw/*.json into one compact gzipped map for the selector.

usage: python compile_map.py MAP_RAW TESTS_JSON OUT.json.gz
Format: {"n": N, "sets": [hex bitsets], "m": {file: {line: idx}}, "f": {...},
         "o": {file: idx}, "unmapped": [nums], "notok": [nums], "subproc": [nums]}
"""
import gzip, json, os, sys

raw, tj, out = sys.argv[1:4]
tests = json.load(open(tj))["tests"]
n = len(tests)
acc = {"m": {}, "f": {}}
opens = {}
recv = {}
unmapped, notok, subproc = [], [], []
for t in tests:
    num = t["number"]
    p = os.path.join(raw, f"{num}.json")
    if not os.path.exists(p):
        unmapped.append(num); continue
    d = json.load(open(p))
    if d["id"] != t["id"]:
        raise SystemExit(f"id mismatch {num}")
    if not d["ok"]:
        notok.append(num)
    if d.get("subproc"):
        subproc.append(num)
    bit = 1 << num
    for kind, key in (("m", "lines"), ("f", "flines")):
        for f, ls in d.get(key, {}).items():
            fm = acc[kind].setdefault(f, {})
            for l in ls:
                fm[l] = fm.get(l, 0) | bit
    for c in d.get("recv", []):
        recv[c] = recv.get(c, 0) | bit
    for f in d["opens"]:
        if f.endswith(".pyc"):
            continue
        opens[f] = opens.get(f, 0) | bit

sets, idx = [], {}
def intern(v):
    if v not in idx:
        idx[v] = len(sets); sets.append(format(v, "x"))
    return idx[v]

res = {"n": n, "m": {}, "f": {}, "o": {}, "unmapped": unmapped, "notok": notok, "subproc": subproc}
for kind in ("m", "f"):
    for f, fm in acc[kind].items():
        res[kind][f] = {str(l): intern(v) for l, v in sorted(fm.items())}
for f, v in opens.items():
    res["o"][f] = intern(v)
res["r"] = {c: intern(v) for c, v in recv.items()}
res["ids"] = [t["id"] for t in tests]
res["sets"] = sets
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
with gzip.open(out, "wt", compresslevel=9) as g:
    json.dump(res, g, separators=(",", ":"))
print("tests", n, "unmapped", len(unmapped), "notok", len(notok), notok[:10], "subproc", len(subproc),
      "sets", len(sets), "bytes", os.path.getsize(out), file=sys.stderr)
