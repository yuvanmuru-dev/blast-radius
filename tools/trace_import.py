"""Trace lines executed by a fresh `python -c CODE` (for subprocess-spawning tests).
usage: python trace_import.py SQLGLOT_DIR RAW_JSON CODE  -> merges lines into RAW_JSON, clears subproc"""
import json, os, sys
src, raw, code = sys.argv[1:4]
SRC = os.path.realpath(src); PREFIX = SRC + os.sep
os.chdir(SRC); sys.path.insert(0, SRC)
M = sys.monitoring; HITS = set()
def on_line(c, l):
    if c.co_filename.startswith(PREFIX):
        HITS.add((c.co_filename[len(PREFIX):], l, c.co_flags & 1))
    return M.DISABLE
M.use_tool_id(M.COVERAGE_ID, "t"); M.register_callback(M.COVERAGE_ID, M.events.LINE, on_line)
M.set_events(M.COVERAGE_ID, M.events.LINE)
exec(compile(code, "<c>", "exec"), {})
M.set_events(M.COVERAGE_ID, 0)
d = json.load(open(raw))
for f, l, k in HITS:
    key = "flines" if k else "lines"
    d[key].setdefault(f, [])
    if l not in d[key][f]:
        d[key][f].append(l)
d["subproc"] = False
json.dump(d, open(raw, "w"))
print("merged", len(HITS))
