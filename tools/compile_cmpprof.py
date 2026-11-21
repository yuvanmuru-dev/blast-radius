"""Aggregate comparison-site profiles (/tmp/brprof/*.txt) -> maps/cmpprof.json.gz"""
import glob, gzip, json, sys
src, out = sys.argv[1], sys.argv[2]
sites = {}
for f in glob.glob(src + "/*.txt"):
    for ln in open(f):
        ln = ln.rstrip("\n")
        if "\t" in ln:
            s, fl = ln.split("\t", 1)
            sites.setdefault(s, set()).add(fl)
res = {s: ",".join(sorted(v)) for s, v in sites.items()}
with gzip.open(out, "wt", compresslevel=9) as g:
    json.dump(res, g, separators=(",", ":"))
eqsafe = sum(1 for v in sites.values() if not ({"isne", "eqerr", "eqnonbool"} & v))
print("sites", len(res), "is/== safe", eqsafe)
