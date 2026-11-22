"""Aggregate the per-test value profile -> maps/vprof.json.gz

usage: compile_vprof.py OUTDIR JUNIT.xml [JUNIT.xml ...] MAPS/vprof.json.gz
A test counts as profiled only if every junit entry for it passed.
"""
import glob, gzip, json, os, sys
import xml.etree.ElementTree as ET

src, *junits, out = sys.argv[1:]
flags = {}
for f in glob.glob(os.path.join(src, "*.txt")):
    if os.path.basename(f).startswith(("inst_", "status_")):
        continue
    for ln in open(f):
        parts = ln.rstrip("\n").split("\t")
        if len(parts) == 3:
            site, fl, key = parts
            flags.setdefault(site, {}).setdefault(fl, set()).add(key)
inst = set()
for f in glob.glob(os.path.join(src, "inst_*.txt")):
    inst |= {x.strip() for x in open(f) if x.strip()}
status = {}
for f in glob.glob(os.path.join(src, "status_*.txt")):
    for ln in open(f):
        tid, _, st = ln.rstrip("\n").partition("\t")
        status[tid] = status.get(tid, True) and st == "OK"
for j in junits:
    if not os.path.exists(j):
        continue
    for tc in ET.parse(j).iter("testcase"):
        cls = tc.get("classname", "")
        mod, _, klass = cls.rpartition(".")
        # classname is "tests.test_x.TestX" (or "tests.test_x" for functions)
        if klass[:1].isupper():
            tid = mod.replace(".", "/") + ".py::" + klass + "::" + tc.get("name")
        else:
            tid = cls.replace(".", "/") + ".py::" + tc.get("name")
        bad = any(c.tag in ("failure", "error", "skipped") for c in tc)
        status[tid] = status.get(tid, True) and not bad
ok = sorted(t for t, v in status.items() if v)
res = {"flags": {s: {f: sorted(k) for f, k in d.items()} for s, d in flags.items()},
       "inst": sorted(inst), "ok": ok}
with gzip.open(out, "wt", compresslevel=9) as g:
    json.dump(res, g, separators=(",", ":"))
print("sites", len(flags), "inst", len(inst), "ok tests", len(ok), "of", len(status))
