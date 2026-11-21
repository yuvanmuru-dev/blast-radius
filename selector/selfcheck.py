"""Build-time sanity check: map matches the checkout and loads."""
import gzip, json, sys
from pathlib import Path
m = json.load(gzip.open("/app/maps/map.json.gz", "rt"))
src = Path("/opt/sqlglot")
missing = [f for f in list(m["m"])[:2000] if not (src / f).exists()]
if missing:
    sys.exit(f"map files missing from checkout: {missing[:5]}")
print("selfcheck ok:", m["n"], "tests,", len(m["m"]), "files")
