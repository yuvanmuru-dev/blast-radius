"""Add per-test set-membership reads (setmap.py output) to maps/map.json.gz.

usage: python merge_setmap.py MAP.json.gz TESTS_JSON SETMAP_DIR
Adds "sa": {"owner\\tattr": {element_digest: set_index}} and "sets_ok" (every
test ran under the instrumentation and passed).
"""
import glob, gzip, json, os, sys

mp, tests_json, sdir = sys.argv[1:4]
d = json.load(gzip.open(mp, "rt"))
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
sets = d["sets"]
index = {h: i for i, h in enumerate(sets)}


def intern(bits):
    hx = format(bits, "x")
    i = index.get(hx)
    if i is None:
        i = len(sets)
        sets.append(hx)
        index[hx] = i
    return i


sa = {}
seen = set()
ok = True
for f in glob.glob(os.path.join(sdir, "*.json")):
    r = json.load(open(f))
    n = num.get(r["id"])
    if n is None:
        continue
    seen.add(n)
    ok = ok and bool(r["ok"])
    for o, a, kd in r["k"]:
        e = sa.setdefault(f"{o}\t{a}", {})
        e[kd] = e.get(kd, 0) | (1 << n)
missing = set(num.values()) - seen
d["sa"] = {e: {kd: intern(v) for kd, v in m.items()} for e, m in sa.items()}
d["sets_ok"] = ok and not missing
with gzip.open(mp, "wt", compresslevel=9) as g:
    json.dump(d, g, separators=(",", ":"))
print("set attrs", len(sa), "sets_ok", d["sets_ok"], "missing", len(missing), "not ok", "?" if ok else "some")
