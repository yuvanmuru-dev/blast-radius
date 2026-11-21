"""Build-time: baseline import fingerprint of the pinned checkout (run twice,
must match) -> maps/fp_base.json. Uses the same /tmp/ov overlay path as runtime."""
import json, os, subprocess, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import verify

src = sys.argv[1] if len(sys.argv) > 1 else "/opt/sqlglot"
out = sys.argv[2] if len(sys.argv) > 2 else str(HERE.parent / "maps" / "fp_base.json")
res = []
for i in range(2):
    ov = verify.build_overlay(src, "/tmp/ov", {})
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=str(ov), PYTHONDONTWRITEBYTECODE="1")
    tmp = f"/tmp/fpb{i}.json"
    subprocess.run([sys.executable, "-S", str(HERE / "fp.py"), tmp], cwd=str(ov), env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res.append(json.loads(Path(tmp).read_text()))
a, b = res
bad = [k for k in a["fp"] if a["fp"][k] != b["fp"].get(k)]
if bad or a["failed"]:
    sys.exit(f"fingerprint not stable: {bad[:5]} failed={a['failed'][:5]}")
Path(out).write_text(json.dumps(a))
# static import graph (who imports whom) for the fingerprint's module subset
os.environ["BR_SQLGLOT"] = src
os.environ["BR_IMPORTERS"] = "/nonexistent"
import main as _m
_m.affected_modules([])
Path(out).with_name("importers.json").write_text(json.dumps({k: sorted(v) for k, v in _m._IMPORTERS[0].items()}))
print("fp_base ok:", len(a["fp"]), "keys")
