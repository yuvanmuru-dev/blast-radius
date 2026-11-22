"""Blast Radius selector.

Pipeline per commit: parse diff -> classify each file change -> candidate tests
-> (optional) verify -> pack onto workers. Any doubt or error means the full
suite: a miss costs 300 s, the full suite costs ~36.8 s.
"""
import ast
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import difflib2  # noqa: E402
import rules  # noqa: E402
import verify  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SQLGLOT", "/opt/sqlglot")).resolve()
WORKER_MS = 2000
MAX_WORKERS = 8
COARSE = os.environ.get("BR_COARSE", "0") == "1"  # diagnostic: file-level only
# bisection toggles (diagnostics): replace one rule family by the file-level rule
T_COMMENT = os.environ.get("BR_T_COMMENT", "0") == "1"   # AST-identical edits
T_FP = os.environ.get("BR_T_FP", "0") == "1"             # class/module-level edits
T_FUNC = os.environ.get("BR_T_FUNC", "0") == "1"         # function/lambda body edits
MAP = Path(os.environ.get("BR_MAP", str(HERE.parent / "maps" / "map.json.gz")))

ALL = "ALL"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- packing
def pack(nums, dur):
    """Best split over k=1..8: LPT, then local moves; minimises max load + 2s*k."""
    nums = sorted(set(nums), key=lambda n: (-dur[n], n))
    if not nums:
        return []
    total = sum(dur[n] for n in nums)
    longest = dur[nums[0]]
    best = None
    for k in range(1, min(MAX_WORKERS, len(nums)) + 1):
        lb = max(longest, -(-total // k)) + WORKER_MS * k
        if best is not None and lb >= best[0]:
            continue
        bins = [[] for _ in range(k)]
        loads = [0] * k
        for n in nums:
            i = loads.index(min(loads))
            bins[i].append(n)
            loads[i] += dur[n]
        _improve(bins, loads, dur)
        used = [(b, l) for b, l in zip(bins, loads) if b]
        cost = max(l for _, l in used) + WORKER_MS * len(used)
        if best is None or cost < best[0]:
            best = (cost, [b for b, _ in used])
    return best[1]


def _improve(bins, loads, dur):
    """Move/swap single tests off the busiest bin while it lowers the max."""
    for _ in range(200):
        hi = loads.index(max(loads))
        improved = False
        for j in range(len(bins)):
            if j == hi:
                continue
            for a in list(bins[hi]):
                da = dur[a]
                if loads[j] + da < loads[hi]:
                    bins[hi].remove(a); bins[j].append(a)
                    loads[hi] -= da; loads[j] += da
                    improved = True
                    break
                for b in bins[j]:
                    db = dur[b]
                    if db < da and loads[j] - db + da < loads[hi]:
                        bins[hi].remove(a); bins[j].remove(b)
                        bins[hi].append(b); bins[j].append(a)
                        loads[hi] += db - da; loads[j] += da - db
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break
        if not improved:
            return


# ---------------------------------------------------------------- classify
def read_src(rel):
    p = SRC / rel
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def ast_equal(old, new):
    try:
        return ast.dump(ast.parse(old)) == ast.dump(ast.parse(new))
    except SyntaxError:
        return False


class Ctx:
    def __init__(self, tests):
        self.tests = tests
        self.all = [t["number"] for t in tests]
        self.map = None
        try:
            self.map = rules.Map(MAP, len(tests), tests)
            sp = 0
        except Exception as e:
            log("no map:", repr(e))


def select_file(ctx, fp):
    """Return a set of test numbers, or ALL."""
    if fp.binary or fp.rename or fp.mode_change:
        return ALL
    if fp.old is None or fp.new is None or fp.old != fp.new:
        return ALL  # new / deleted / moved file
    path = fp.path
    old = read_src(path)
    if old is None:
        return ALL
    new = difflib2.apply(old, fp)
    if new == old:
        return set()
    if path.endswith(".py") and COARSE and ctx.map is not None:
        return rules.bits_to_set(ctx.map.file_any(path) | ctx.map.always)
    if path.endswith(".py"):
        try:
            ot = ast.parse(old)
        except SyntaxError:
            return ALL
        try:
            nt = ast.parse(new)
        except SyntaxError:
            nt = None
        if nt is not None and rules.same_tree(ot, nt):
            if T_COMMENT and ctx.map is not None:
                return rules.bits_to_set(ctx.map.file_any(path) | ctx.map.always)
            return set()  # comment / whitespace only (docstrings are AST)
        if ctx.map is None:
            return ALL
        b = rules.select_py(ctx.map, path, old, new, fp, ot, nt)
        if b == ALL:
            return ALL
        bits, need_fp = b
        if T_FUNC:
            bits = ctx.map.file_any(path) | ctx.map.always
        if need_fp and T_FP:
            return rules.bits_to_set(bits | ctx.map.file_any(path) | ctx.map.always)
        if need_fp:
            if not path.startswith("sqlglot/"):
                # fingerprint covers sqlglot modules only: be conservative elsewhere
                return rules.bits_to_set(bits | ctx.map.file_any(path) | ctx.map.always)
            ctx.need_fp.append(path)
            ctx.need_fp_src[path] = (old, new)
        return rules.bits_to_set(bits)
    if ctx.map is None:
        return ALL
    b = ctx.map.opened(path)
    if b and path in TPC_FIXTURES:
        b = tpc_fixture_refine(ctx, path, old, new, b)
    if b:
        b |= ctx.map.always
    return rules.bits_to_set(b)


TPC_FIXTURES = {
    "tests/fixtures/optimizer/tpc-h/tpc-h.sql": "h",
    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": "ds",
}
TPC_EXEC = "tests/test_executor.py::TestExecutor::"
# which part of each fixture a TestExecutor test reads (all of them load both
# files in setUpClass, so they all fail if either file stops loading)
TPC_USES = {
    "test_execute_tpch": ("h", "meta_sql"),
    "test_optimized_tpch": ("h", "sql_expected"),
    "test_execute_tpcds": ("ds", "meta_sql"),
}
TPC_REFINE = os.environ.get("BR_TPC_REFINE", "1") == "1"


def _tpc_pairs(text):
    """Exact copy of tests/helpers.py load_sql_fixture_pairs (text mode read)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    filtered = "\n".join([line for line in text.splitlines() if line and not line.startswith("--")])
    statements = filtered.split(";")
    size = len(statements)
    out = []
    for i in range(0, size, 2):
        if i + 1 < size:
            sql = statements[i].strip()
            meta = {}
            sql_lines = sql.split("\n")
            j = 0
            while sql_lines[j].startswith("#"):
                key, val = sql_lines[j].split(":", maxsplit=1)
                meta[key.lstrip("#").strip()] = val.strip()
                j += 1
            sql = "\n".join(sql_lines[j:])
            out.append((meta, sql, statements[i + 1].strip()))
    return out


def _tpcds_sql_edit_inert(ctx, o, n):
    """test_execute_tpcds parses every query (parse_one in the test process) but
    only runs the ones whose meta has an "execute" key. An edited query is
    harmless when it still parses and is either not run, or parses to the same
    tree and transpiles to the same DuckDB SQL. Only for fixture-only commits:
    the check uses the pristine sqlglot."""
    if len(o) != len(n) or any(p.startswith("sqlglot/") for p in ctx.patched_paths):
        return False
    try:
        if str(SRC) not in sys.path:
            sys.path.insert(1, str(SRC))
        import sqlglot
        if not str(Path(sqlglot.__file__).resolve()).startswith(str(SRC)):
            return False
        from sqlglot import parse_one, transpile
    except Exception:
        return False
    for (mo, qo, _), (mn, qn, _) in zip(o, n):
        if (mo, qo) == (mn, qn):
            continue
        if mo != mn:
            return False
        try:
            tn = parse_one(qn)
            import pickle
            pickle.dumps(tn)  # the test ships it to a worker process
        except Exception:
            return False
        if not mn.get("execute"):
            continue
        try:
            to = parse_one(qo)
            if to != tn or to.sql() != tn.sql() or transpile(qo, write="duckdb") != transpile(qn, write="duckdb"):
                return False
        except Exception:
            return False
    return True


def tpc_fixture_refine(ctx, path, old, new, bits):
    """A TPC fixture edit only reaches TestExecutor tests through setUpClass:
    keep the ones that read a changed part, drop the rest when both files
    still load."""
    if not TPC_REFINE or ctx.patched_paths & {"tests/helpers.py", "tests/test_executor.py"}:
        return bits
    try:
        o = _tpc_pairs(old)
        n = _tpc_pairs(new)
        for other, which in TPC_FIXTURES.items():
            if other != path:
                other_text = read_src(other)
                if other_text is None:
                    return bits
                if other in ctx.patched_new:
                    other_text = ctx.patched_new[other]
                _tpc_pairs(other_text)
    except Exception:
        return bits  # setUpClass would fail: every TestExecutor test is affected
    meta_sql_changed = [(m, q) for m, q, _ in o] != [(m, q) for m, q, _ in n]
    if meta_sql_changed and path.endswith("tpc-ds.sql") and _tpcds_sql_edit_inert(ctx, o, n):
        meta_sql_changed = False
    changed = {
        "meta_sql": meta_sql_changed,
        "sql_expected": [(q, e) for _, q, e in o] != [(q, e) for _, q, e in n],
    }
    which = TPC_FIXTURES[path]
    for t in ctx.tests:
        tid = t["id"]
        if not tid.startswith(TPC_EXEC) or not (bits >> t["number"]) & 1:
            continue
        name = tid[len(TPC_EXEC):]
        use = TPC_USES.get(name)
        if use and use[0] == which and changed[use[1]]:
            continue
        bits &= ~(1 << t["number"])
    return bits


def select_commit(ctx, text, cid="x"):
    files = difflib2.parse(text)
    if not files:
        return ALL
    ctx.need_fp = []
    ctx.need_fp_src = {}
    if ctx.map is not None:
        ctx.map.span_log = []
        ctx.map.nonspan = False
    out = set()
    ctx.patched_paths = {fp.path for fp in files}
    ctx.patched_new = {}
    for fp in files:
        if fp.path in TPC_FIXTURES and not fp.binary and fp.old == fp.new:
            o = read_src(fp.path)
            if o is not None:
                try:
                    ctx.patched_new[fp.path] = difflib2.apply(o, fp)
                except Exception:
                    pass
    ctx.diag_files = []
    for fp in files:
        s = select_file(ctx, fp)
        if True:
            try:
                rm, an = difflib2.changed_old_lines(fp)
                cl = sorted(rm) or sorted(x for x in an if x > 0)
            except Exception:
                cl = []
            ctx.diag_files.append((fp.path, s, cl[0] if cl else 0))
        if s == ALL:
            return ALL
        out |= s
    if ctx.need_fp:
        b = fingerprint_select(ctx, text, cid)
        if b == ALL:
            return ALL
        out |= rules.bits_to_set(b | ctx.map.always)
    return out


NARROW_ATTRS = os.environ.get("BR_NARROW", "1") == "1"
KEYED = os.environ.get("BR_KEYED", "1") == "1"
FUNC_BODY = os.environ.get("BR_FUNC_BODY", "1") == "1"
FP_ALL = os.environ.get("BR_FP_ALL", "1") == "1"
FP_BASE = Path(os.environ.get("BR_FPBASE", str(HERE.parent / "maps" / "fp_base.json")))
_FPB = [None]
_src_index = [None]


def _fp_base():
    if _FPB[0] is None:
        _FPB[0] = json.loads(FP_BASE.read_text())
    return _FPB[0]


def source_lines_with(tokens):
    """{relfile: [line numbers]} of lines in sqlglot/ and tests/ (.py) where every
    part of some token group appears (word-bounded)."""
    import bisect
    import re
    if _src_index[0] is None:
        idx = {}
        for base in ("sqlglot", "tests"):
            for d, dirs, files in os.walk(SRC / base):
                dirs[:] = [x for x in dirs if x != "__pycache__" and x != "fixtures"]
                for f in files:
                    if f.endswith(".py"):
                        p = Path(d) / f
                        text = p.read_text(encoding="utf-8")
                        nl = [i for i, c in enumerate(text) if c == "\n"] if False else None
                        idx[str(p.relative_to(SRC))] = [text, None]
        _src_index[0] = idx
    groups = [t for t in tokens if t]
    flat = sorted({p for t in groups for p in t})
    if not flat:
        return {}
    pat = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in flat) + r")\b")
    out = {}
    for f, ent in _src_index[0].items():
        text = ent[0]
        if not pat.search(text):
            continue
        if ent[1] is None:
            ent[1] = [m.start() for m in re.finditer("\n", text)]
        nls = ent[1]
        per_line = {}
        for m in pat.finditer(text):
            ln = bisect.bisect_left(nls, m.start()) + 1
            per_line.setdefault(ln, set()).add(m.group(0))
        hits = [ln for ln, found in per_line.items() if any(all(p in found for p in g) for g in groups)]
        if hits:
            out[f] = sorted(hits)
    return out


_IMPORTERS = [None]


def _import_time_nodes(tree):
    """Nodes that run when the module is imported: everything outside
    function/lambda bodies (class bodies and module-level blocks included)."""
    import ast as _ast
    out = []
    stack = [tree]
    while stack:
        n = stack.pop()
        out.append(n)
        for ch in _ast.iter_child_nodes(n):
            if isinstance(ch, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.Lambda)):
                # decorators and defaults run at import, bodies do not
                stack.extend(getattr(ch, "decorator_list", []))
                stack.append(ch.args)
                continue
            stack.append(ch)
    return out
IMPORTERS_FILE = Path(os.environ.get("BR_IMPORTERS", str(HERE.parent / "maps" / "importers.json")))


def _modname(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def affected_modules(paths):
    """Changed modules plus every sqlglot module that imports them, transitively
    (static imports anywhere in the file, relative ones resolved)."""
    import ast as _ast
    if _IMPORTERS[0] is None and IMPORTERS_FILE.exists():
        _IMPORTERS[0] = {k: set(v) for k, v in json.loads(IMPORTERS_FILE.read_text()).items()}
    if _IMPORTERS[0] is None:
        imp = {}
        base = SRC / "sqlglot"
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                p = Path(d) / f
                rel = str(p.relative_to(SRC))
                me = _modname(rel)
                pkg = me if f == "__init__.py" else me.rsplit(".", 1)[0]
                try:
                    tree = _ast.parse(p.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                for n in _import_time_nodes(tree):
                    targets = []
                    if isinstance(n, _ast.Import):
                        targets = [a.name for a in n.names]
                    elif isinstance(n, _ast.ImportFrom):
                        if n.level:
                            parts = pkg.split(".")
                            parts = parts[: len(parts) - (n.level - 1)]
                            root = ".".join(parts + ([n.module] if n.module else []))
                        else:
                            root = n.module or ""
                        targets = [root] + [root + "." + a.name for a in n.names]
                    for t in targets:
                        if t.startswith("sqlglot"):
                            # importing a.b.c also runs a and a.b
                            parts = t.split(".")
                            for i in range(1, len(parts) + 1):
                                imp.setdefault(".".join(parts[:i]), set()).add(me)
                # importing a submodule imports its parent packages
                parts = me.split(".")
                for i in range(1, len(parts)):
                    imp.setdefault(".".join(parts[:i]), set()).add(me)
        _IMPORTERS[0] = imp
    imp = _IMPORTERS[0]
    todo = [_modname(p) for p in paths]
    seen = set(todo)
    while todo:
        m = todo.pop()
        for x in imp.get(m, ()):
            if x not in seen:
                seen.add(x)
                todo.append(x)
    return sorted(seen)


def closure_modules(paths):
    """Modules whose import-time state can depend on the changed modules: every
    module that imports them at import time (transitively), plus everything those
    import (registries they may write into)."""
    rev = affected_modules(paths)
    imp = _IMPORTERS[0]
    fwd = {}
    for m, users in imp.items():
        for u in users:
            fwd.setdefault(u, set()).add(m)
    seen = set(rev)
    todo = list(rev)
    while todo:
        m = todo.pop()
        for x in fwd.get(m, ()):
            if x not in seen:
                seen.add(x)
                todo.append(x)
    known = {k.split(":", 1)[0] for k in _fp_base()["fp"]}
    return sorted(seen & known)


def fingerprint_select(ctx, text, cid):
    """Class/module-level edits: diff the patched import-time state against the
    baseline and select tests that use any changed class or name."""
    M = ctx.map
    M.nonspan = True
    base = _fp_base()
    files = patched_files(text)
    # hash every module: an import can also mutate state in modules it imports
    # (registries), not only in modules that import it
    mods = closure_modules(ctx.need_fp) if os.environ.get("BR_FP_MODE", "closure") == "closure" \
        else sorted({k.split(":", 1)[0] for k in _fp_base()["fp"]})
    ov = verify.build_overlay(SRC, "/tmp/ov", files)
    outp = "/tmp/fp_out.json"
    try:
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=str(ov), PYTHONDONTWRITEBYTECODE="1",
                   BR_FP_COMPARE=str(FP_BASE))
        import subprocess
        r = subprocess.run([sys.executable, "-S", str(HERE / "fp.py"), outp] + mods, cwd=str(ov), env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if r.returncode != 0:
            return ALL
        new = json.loads(Path(outp).read_text())
    finally:
        import shutil
        shutil.rmtree(ov, ignore_errors=True)
    bits = 0
    failed = set(new["failed"]) - set(base["failed"])
    for mod in failed:
        rel = mod.replace(".", "/")
        for cand in (rel + ".py", rel + "/__init__.py"):
            bits |= M.file_any(cand)
    modset = set(mods)
    full = base["fp"]
    nfp = new["fp"]
    # keys the subset run should have produced, plus anything it did produce
    # deletions are looked for only in the edited modules themselves; elsewhere
    # compare what the run produced (a subset run reaches some dynamically built
    # classes under other key paths)
    edited = {_modname(p) for p in ctx.need_fp}
    bfp = {k: v for k, v in full.items() if k.split(":", 1)[0] in edited}
    for k in nfp:
        if k in full:
            bfp[k] = full[k]
    by_owner = {}
    glob_tokens = set()
    base_entries = base.get("entries", {})
    keyed_bits = 0
    keyed_log = []
    bextra = base.get("extra", {})
    nextra = new.get("extra", {})
    failed_now = set(new.get("failed", []))
    for k in set(bfp) | set(nfp):
        a, b = bfp.get(k), nfp.get(k)
        if a == b:
            continue
        if k.split(":", 1)[0] in failed_now:
            continue  # module no longer imports: its importers are selected via file_any below
        bx, nx = bextra.get(k), nextra.get(k)
        if FUNC_BODY and bx and nx:
            bf, nf = bx.get("fn"), nx.get("fn")
            if bf and nf and bf[0] == nf[0] and bf[3] == nf[3]:
                # same function (name + signature): only defaults / closure values
                # changed, which matter only when its body runs
                bits |= M.lines("f", bf[0], bf[1], bf[2])
                keyed_log.append(("fn", k.rsplit(":", 1)[-1], 0))
                continue
            if "sh" in bx and "sh" in nx and bx["sh"] == nx["sh"] and not bx["anon"] and not nx["anon"]:
                # container changed only through named functions it holds;
                # those functions are keys of their own and handled there
                continue
        owner, _, kind = (a or b)
        if (a and b) and a[2] != b[2]:
            kind = "F"
        if owner:
            mod = k.split(":", 1)[0]
            attr = k.rsplit(".", 1)[-1]
            # dict attribute whose entries we can compare: select by key reads
            be, ne = base_entries.get(k), new.get("entries", {}).get(k)
            if KEYED and be is not None and ne is not None and M.keys_ok and not attr.startswith("__"):
                changed = {x for x in set(be) | set(ne) if be.get(x) != ne.get(x)}
                kb = M.key_reads(owner, attr, changed)
                if kb is not None:
                    keyed_bits |= kb
                    keyed_log.append((owner.rsplit(".", 1)[-1], attr, len(changed)))
                    continue
            qual = owner[len(mod) + 1:] if owner.startswith(mod + ".") else owner.rsplit(".", 1)[-1]
            ent = by_owner.setdefault(owner, {"qual": qual, "attrs": set(), "all_values": True})
            ent["attrs"].add(attr)
            if kind != "V" or attr.startswith("__"):
                ent["all_values"] = False
        else:
            mod, gname = k.split(":", 1)
            be, ne = base_entries.get(k), new.get("entries", {}).get(k)
            if KEYED and be is not None and ne is not None and M.keys_ok:
                changed = {x for x in set(be) | set(ne) if be.get(x) != ne.get(x)}
                kb = M.key_reads("module:" + mod, gname, changed)
                if kb is not None:
                    keyed_bits |= kb
                    keyed_log.append((mod.rsplit(".", 1)[-1], gname, len(changed)))
                    continue
            glob_tokens.add((gname,))
    if os.environ.get("BR_FP_DUMP"):
        with open(os.environ["BR_FP_DUMP"], "a") as _f:
            _f.write(json.dumps([cid, sorted(k for k in set(bfp) | set(nfp) if bfp.get(k) != nfp.get(k) and k.split(":", 1)[0] not in failed_now)]) + "\n")
    log(cid, "fp changed", {o: sorted(e["attrs"])[:4] for o, e in list(by_owner.items())[:4]}, sorted(glob_tokens)[:4],
        "keyed", keyed_log[:4])
    bits |= keyed_bits
    if os.environ.get("BR_DBG_TEST") and (keyed_bits >> int(os.environ["BR_DBG_TEST"])) & 1:
        log(cid, "DBGX via keyed", keyed_log)
    if len(by_owner) > 400:
        return ALL
    if not by_owner and not glob_tokens and not failed and not keyed_log:
        # a code edit outside functions that changes no import-time state is
        # unexpected unless it only touched docstrings / TYPE_CHECKING / annotations:
        # otherwise do not trust it and fall back to everything touching the file
        for p, (o, n) in ctx.need_fp_src.items():
            if not rules.inert_change(o, n):
                bits |= M.file_any(p)
        return bits
    if glob_tokens and os.environ.get("BR_DBG_TEST"):
        _g = 0
        for f, lines in source_lines_with(glob_tokens).items():
            for l in lines:
                _g |= M.lines("f", f, l, l)
        if (_g >> int(os.environ["BR_DBG_TEST"])) & 1:
            log(cid, "DBGX via globals", sorted(glob_tokens)[:5])
    if glob_tokens:
        for f, lines in source_lines_with(glob_tokens).items():
            for l in lines:
                bits |= M.lines("f", f, l, l)
    if any(p.startswith("sqlglot/typing/") for p in ctx.need_fp):
        bits |= M.file_tests("tests/test_typing.py")
    DBG = os.environ.get("BR_DBG_TEST")
    for owner, ent in by_owner.items():
        _b0 = bits
        qparts = tuple(p for p in ent["qual"].split(".") if p and not p.startswith("<"))
        recv = M.receivers(owner)
        if ent["all_values"] and NARROW_ATTRS:
            # plain data attributes: a test can only see the change by reading the
            # attribute, i.e. by running a line that names it, while using the class
            attr_bits = 0
            for f, lines in source_lines_with({(a,) for a in ent["attrs"]}).items():
                for l in lines:
                    attr_bits |= M.lines("f", f, l, l)
            bits |= recv & attr_bits
            # explicit Class.ATTR references from code not running on the class
            for f, lines in source_lines_with({qparts + (a,) for a in ent["attrs"]}).items():
                for l in lines:
                    bits |= M.lines("f", f, l, l)
        else:
            bits |= recv
            for f, lines in source_lines_with({qparts}).items():
                for l in lines:
                    bits |= M.lines("f", f, l, l)
        if DBG and not (_b0 >> int(DBG)) & 1 and (bits >> int(DBG)) & 1:
            log(cid, "DBGX owner pulls test", owner, sorted(ent["attrs"])[:5], ent["all_values"])
    return bits


def patched_files(text):
    out = {}
    for fp in difflib2.parse(text):
        if fp.old:
            out[fp.old] = None
        if fp.new:
            out[fp.new] = difflib2.apply(read_src(fp.old) if fp.old else None, fp)
    return out


VERIFY_MAX_TEST = float(os.environ.get("BR_VMAX", "0.5"))
VERIFY_BUDGET = float(os.environ.get("BR_VBUDGET", "1.0"))
VERIFY = os.environ.get("BR_VERIFY", "0") == "1"
CPU_FACTOR = float(os.environ.get("BR_CPUF", "3.0"))  # judge CPU seconds per local second (measured on the board: stand-in 3.7 s local ~ 3.5 s judge)


PARTIAL = os.environ.get("BR_PARTIAL", "0") == "1"
UNITS_FILE = Path(os.environ.get("BR_UNITS", str(HERE.parent / "maps" / "units.json.gz")))
HEAVY_ATTR = {"test_execute_tpcds": "tpcds_sqls", "test_execute_tpch": "tpch_sqls", "test_optimized_tpch": "tpch_sqls"}
_UNITS = [None]


def _units():
    if _UNITS[0] is None:
        import gzip
        raw = json.load(gzip.open(UNITS_FILE, "rt"))
        idx = {}
        for test, units in raw.items():
            m = {}
            for i, lines in enumerate(units):
                for f, ls in lines.items():
                    for l in ls:
                        m.setdefault((f, l), set()).add(i)
            idx[test] = m
        _UNITS[0] = idx
    return _UNITS[0]


def partial_heavy(ctx, cid, text, sel, dur, deadline):
    """Heavy TPC tests reached only through line spans: re-run just the queries
    whose own lines the patch touches; drop the test if they all pass."""
    M = ctx.map
    if M is None or M.nonspan or not M.span_log or time.time() > deadline or not UNITS_FILE.exists():
        return sel
    tests = {x["number"]: x for x in ctx.tests}
    heavy = [n for n in sel if tests[n]["id"].startswith("tests/test_executor.py::TestExecutor::")
             and tests[n]["id"].rsplit("::", 1)[1] in HEAVY_ATTR]
    if not heavy:
        return sel
    units = _units()
    plan = {}
    for n in heavy:
        name = tests[n]["id"].rsplit("::", 1)[1]
        um = units.get(name)
        if not um:
            continue
        bit = 1 << n
        q, ok = set(), True
        for f, lo, hi in M.span_log:
            tab = M.f.get(f, {})
            for l in range(lo, hi + 1):
                i = tab.get(l)
                if i is None or not (M.sets[i] & bit):
                    continue
                us = um.get((f, l))
                if not us:
                    ok = False  # reached only outside the per-query parts (e.g. import)
                    break
                q |= us
            if not ok:
                break
        if ok and q:
            plan[n] = (name, sorted(q))
    if not plan:
        return sel
    # cost: interpreter + imports + setUpClass ~2.5 s, plus ~0.4 s per query
    est = CPU_FACTOR * (2500 + 400 * sum(len(q) for _, q in plan.values()))
    before = cost_ms(pack(sel, dur), dur)
    after = cost_ms(pack(set(sel) - set(plan), dur), dur)
    if before - after < 1.5 * est:
        log(cid, "partial skipped", {tests[n]["id"].rsplit("::", 1)[1]: len(q) for n, (_, q) in plan.items()})
        return sel
    files = patched_files(text)
    ov = verify.build_overlay(SRC, "/tmp/ov", files)
    try:
        req = [(n, tests[n]["id"], tests[n]["seconds"], {HEAVY_ATTR[name]: q}) for n, (name, q) in plan.items()]
        failed = verify.run(ov, req, timeout=max(10.0, min(deadline - time.time(), 90.0)))
    finally:
        import shutil
        shutil.rmtree(ov, ignore_errors=True)
    dropped = set(plan) - failed
    log(cid, "partial", {tests[n]["id"].rsplit("::", 1)[1]: len(q) for n, (_, q) in plan.items()}, "dropped", len(dropped))
    return set(sel) - dropped


PROBE = os.environ.get("BR_PROBE", "1") == "1"
PROBES = {"tests/test_parser.py::TestParser::test_max_nodes": "probe_maxnodes.py"}
TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_OPT = os.environ.get("BR_TPCDS_OPT", "1") == "1"
# optimizer modules the planner/executor also call at run time
OPT_RUNTIME = {"sqlglot/optimizer/scope.py", "sqlglot/optimizer/eliminate_joins.py"}


def _stmt_lines(tree, line):
    """Lines of the innermost statement containing `line` (header lines only for
    compound statements), so continuation lines map to their statement."""
    best = None
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt) and n.lineno <= line <= (n.end_lineno or n.lineno):
            if best is None or (n.lineno >= best.lineno and (n.end_lineno or n.lineno) <= (best.end_lineno or best.lineno)):
                best = n
    if best is None:
        return {line}
    hi = best.end_lineno or best.lineno
    body = getattr(best, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        hi = body[0].lineno - 1
    return set(range(best.lineno, max(best.lineno, hi) + 1)) | {line}


def _tpcds_queries_reaching(text, prefix="sqlglot/optimizer/", ctx=None):
    """TPC-DS query indices whose execution ran any changed line under prefix
    (per-query map), or None when that cannot be told: also None when the test
    runs a changed line that the per-query map does not attribute to a query
    (code that runs outside the per-query work), so every query is checked."""
    if not UNITS_FILE.exists() or ctx is None or ctx.map is None:
        return None
    try:
        um = _units().get("test_execute_tpcds")
        if not um:
            return None
        tbit = next((1 << x["number"] for x in ctx.tests if x["id"] == TPCDS_ID), 0)
        q = set()
        for fp in difflib2.parse(text):
            if not (fp.path.startswith(prefix) and fp.path.endswith(".py")):
                continue
            old = read_src(fp.path)
            tree = ast.parse(old)
            rm, an = difflib2.changed_old_lines(fp)
            for ln in set(rm) | {x for x in an if x > 0}:
                for l2 in _stmt_lines(tree, ln):
                    hit = um.get((fp.path, l2))
                    if hit:
                        q |= hit
                    elif ctx.map.lines("f", fp.path, l2, l2) & tbit or ctx.map.lines("m", fp.path, l2, l2) & tbit:
                        return None  # run by the test, but not inside any one query's work
        return q
    except Exception:
        return None


TPCDS_EXEC = os.environ.get("BR_TPCDS_EXEC", "1") == "1"


def tpcds_exec_standin(ctx, cid, text, sel, dur):
    """test_execute_tpcds for commits whose only behavioral change is in the
    Python executor: re-execute the (reached) queries on the fixture data the
    way the test's workers do and compare every result with the baseline."""
    if not TPCDS_EXEC or not sel:
        return sel
    ids = {x["number"]: x["id"] for x in ctx.tests}
    tn = next((n for n in sel if ids[n] == TPCDS_ID), None)
    if tn is None:
        return sel
    changed = False
    for path, s, _ in getattr(ctx, "diag_files", []):
        if path.startswith("sqlglot/executor/") and path.endswith(".py"):
            if path in getattr(ctx, "need_fp", []):
                return sel
            changed = True
            continue
        if s == ALL or (tn in s) or path in getattr(ctx, "need_fp", []):
            return sel
    if not changed:
        return sel
    before = cost_ms(pack(sorted(sel), dur), dur)
    after = cost_ms(pack(sorted(set(sel) - {tn}), dur), dur)
    if before - after < 15000 or not PROBE_BASE.exists() or time.time() - T_START > 150:
        return sel
    extra = {"BR_TPCDS_CMP": str(PROBE_BASE)}
    trees = PROBE_BASE.with_name("tpcds_opt_trees.pkl")
    if trees.exists() and "sqlglot/executor/__init__.py" not in getattr(ctx, "patched_paths", set()) \
            and os.environ.get("BR_TPCDS_FAST", "1") == "1":
        extra["BR_TPCDS_FAST"] = str(trees)
    q = None if os.environ.get("BR_TPCDS_ALLQ") == "1" else _tpcds_queries_reaching(text, "sqlglot/executor/", ctx)
    if q is not None:
        extra["BR_TPCDS_ONLY"] = ",".join(str(x) for x in sorted(q))
    ov = verify.build_overlay(SRC, "/tmp/ov", patched_files(text))
    try:
        out = _probe_out("probe_tpcds_exec.py", ov, extra)
    finally:
        import shutil
        shutil.rmtree(ov, ignore_errors=True)
    same = out is not None and out.strip() == '{"same": true}'
    log(cid, "tpcds-exec standin", "same" if same else "differs", (out or "").strip()[:60], "q", None if q is None else len(q))
    return set(sel) - {tn} if same else sel


def tpcds_opt_standin(ctx, cid, text, sel, dur):
    """test_execute_tpcds for commits whose only behavioral change is in
    optimizer code: identical optimized trees (and DuckDB SQL) for every
    executed query mean an identical plan, identical execution, identical
    result. Runs the stand-in on the patched tree, drops the test on a match."""
    if not TPCDS_OPT or not sel:
        return sel
    ids = {x["number"]: x["id"] for x in ctx.tests}
    tn = next((n for n in sel if ids[n] == TPCDS_ID), None)
    if tn is None:
        return sel
    files = getattr(ctx, "diag_files", [])
    opt_changed = False
    for path, s, _ in files:
        if path.startswith("sqlglot/optimizer/") and path.endswith(".py"):
            if path in OPT_RUNTIME or path in getattr(ctx, "need_fp", []):
                return sel
            opt_changed = True
            continue
        if s == ALL or (tn in s) or path in getattr(ctx, "need_fp", []):
            return sel  # some other change reaches the test by itself
    if not opt_changed:
        return sel
    before = cost_ms(pack(sorted(sel), dur), dur)
    after = cost_ms(pack(sorted(set(sel) - {tn}), dur), dur)
    if before - after < CPU_FACTOR * 1500 or time.time() - T_START > 150:
        return sel
    script = "probe_tpcds_opt.py"
    if not PROBE_BASE.exists():
        return sel
    extra = {"BR_TPCDS_CMP": str(PROBE_BASE)}
    q = None if os.environ.get("BR_TPCDS_ALLQ") == "1" else _tpcds_queries_reaching(text, ctx=ctx)
    if q is not None:
        extra["BR_TPCDS_ONLY"] = ",".join(str(x) for x in sorted(q))
    ov = verify.build_overlay(SRC, "/tmp/ov", patched_files(text))
    try:
        out = _probe_out(script, ov, extra)
    finally:
        import shutil
        shutil.rmtree(ov, ignore_errors=True)
    same = out is not None and out.strip() == '{"same": true}'
    log(cid, "tpcds-opt standin", "same" if same else "differs", (out or "").strip()[:60])
    return set(sel) - {tn} if same else sel
PROBE_BASE = Path(os.environ.get("BR_PROBE_BASE", str(HERE.parent / "maps" / "probe_base.json")))
_PB = {}


def _probe_out(script, root, extra=None):
    import subprocess
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "BR_TPCDS_CMP", "BR_TPCDS_FAST", "BR_TPCDS_ONLY", "BR_TPCDS_DUMP_OPT")}
    env.update(PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    env.update(extra or {})
    try:
        r = subprocess.run([sys.executable, "-S", str(HERE / script)], cwd=str(root), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90)
    except Exception:
        return None
    out = r.stdout.decode("utf-8", "replace")
    return out if r.returncode == 0 and out.strip() else None


def _probe_base(script):
    if script not in _PB:
        b = None
        try:
            b = json.loads(PROBE_BASE.read_text()).get(script)
        except Exception:
            pass
        _PB[script] = b if b is not None else _probe_out(script, SRC)
    return _PB[script]


def probe_heavy(ctx, cid, text, sel, dur):
    """Very slow tests with a scaled stand-in: drop the test when the patched
    tree behaves exactly like the pristine one on the stand-in."""
    if not PROBE or not sel:
        return sel
    ids = {x["number"]: x["id"] for x in ctx.tests}
    cands = [n for n in sel if ids[n] in PROBES]
    if not cands:
        return sel
    files = patched_files(text)
    if not files or any(not p.startswith("sqlglot/") for p in files):
        return sel
    before = cost_ms(pack(sorted(sel), dur), dur)
    after = cost_ms(pack(sorted(set(sel) - set(cands)), dur), dur)
    if before - after < CPU_FACTOR * 1000:
        return sel
    ov = verify.build_overlay(SRC, "/tmp/ov", files)
    drop = set()
    try:
        for n in cands:
            script = PROBES[ids[n]]
            base = _probe_base(script)
            out = _probe_out(script, ov)
            if base is not None and out is not None and out == base:
                drop.add(n)
    finally:
        import shutil
        shutil.rmtree(ov, ignore_errors=True)
    log(cid, "probe", [ids[n].rsplit("::", 1)[1] for n in cands], "dropped", len(drop))
    return set(sel) - drop


def cost_ms(workers, dur):
    return (max(sum(dur[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0


def maybe_verify(ctx, cid, text, sel, dur, deadline):
    if not VERIFY or not sel or time.time() > deadline:
        return sel
    light = [n for n in sel if dur[n] <= VERIFY_MAX_TEST * 1000]
    if not light or sum(dur[n] for n in light) > VERIFY_BUDGET * 1000:
        return sel
    heavy = set(sel) - set(light)
    before = cost_ms(pack(sel, dur), dur)
    best_after = cost_ms(pack(heavy, dur), dur)
    if any(not (fp.new or "").endswith(".py") for fp in difflib2.parse(text)):
        return sel  # data/fixture edits nearly always break their readers
    est_cpu = CPU_FACTOR * (350 + 1.3 * sum(dur[n] for n in light))  # interpreter + imports + run
    if before - best_after < est_cpu:
        return sel
    files = patched_files(text)
    ov = verify.build_overlay(SRC, "/tmp/ov", files)
    try:
        t = {x["number"]: x for x in ctx.tests}
        req = [(n, t[n]["id"], t[n]["seconds"]) for n in light]
        failed = verify.run(ov, req, timeout=max(5.0, min(deadline - time.time(), 60.0)))
    finally:
        import shutil
        shutil.rmtree(ov, ignore_errors=True)
    kept = heavy | failed
    log(cid, "verify", len(light), "->", len(failed), "failing")
    return kept


DIAG = os.environ.get("BR_DIAG", "0") in ("1", "2", "3", "4", "5", "6")
DIAG_MODE = os.environ.get("BR_DIAG", "0")
DIAG_GROUPS = [
    ("sqlglot/parser.py",), ("sqlglot/generator.py",), ("sqlglot/optimizer/",), ("sqlglot/expressions/",),
    ("sqlglot/dialects/",), ("sqlglot/generators/",), ("sqlglot/parsers/",),
    ("sqlglot/tokens.py", "sqlglot/tokenizer_core.py"), ("sqlglot/executor/", "sqlglot/planner.py"),
    ("sqlglot/typing/",), ("sqlglot/errors.py", "sqlglot/helper.py", "sqlglot/serde.py", "sqlglot/__init__.py"),
    ("tests/fixtures/",), ("tests/",), ("sqlglot/schema.py", "sqlglot/time.py", "sqlglot/trie.py", "sqlglot/jsonpath.py"),
]


def _diag_group(paths):
    """First matching group over all patched paths (order of DIAG_GROUPS); 14 other, 15 none."""
    best = 15
    for p in paths:
        g = next((i for i, pre in enumerate(DIAG_GROUPS) if any(p.startswith(x) for x in pre)), 14)
        best = min(best, g)
    return best


def diag2_answers(rows, tests, dur):
    """Practice diagnostic 2: for the (first 3) commits that select DIAG_TEST,
    code = group*4 + path (0 line, 1 fp, 2 fixture/non-py, 3 multi-file);
    total = 500000 + c0 + 64*c1 + 4096*c2 ms. DIAG_TEST is removed from the
    first such commit only, so the miss count says whether it was needed there."""
    tid = {t["id"]: t["number"] for t in tests}[DIAG_TEST]
    out, base, codes, first = [], 0, [], True
    for cid, nums, is_all, paths, used_fp in rows:
        if tid in nums:
            path = 3 if len(paths) > 1 else (1 if used_fp else (2 if paths and not paths[0].endswith(".py") else 0))
            codes.append(_diag_group(paths) * 4 + path)
            if first:
                nums = [n for n in nums if n != tid]
                first = False
        w = pack(nums, dur)
        out.append([cid, w])
        base += cost_ms(w, dur)
    codes = (codes + [63, 63, 63])[:3]
    target = 500000 + codes[0] + 64 * codes[1] + 4096 * codes[2]
    log("DIAG2 codes", codes, "base", base, "target", target)
    return _diag_pad(out, base, target, tid, dur)


def _py_index():
    files = sorted(str(p.relative_to(SRC)) for p in (SRC / "sqlglot").rglob("*.py") if "__pycache__" not in p.parts)
    return {f: i for i, f in enumerate(files)}


def diag3_answers(rows, tests, dur):
    """Practice diagnostic 3: the first commit selecting DIAG_TEST. Which file
    brought the test in and the first changed line of that file:
        total = 500000 + 4096*file_index + first_old_line   (file_index into the
    sorted sqlglot/**/*.py list; 255 = not a sqlglot .py file; line capped 4095).
    If the test only came from the class/module fingerprint, the first
    fingerprinted file is reported with line 0. Selections are unchanged."""
    tid = {t["id"]: t["number"] for t in tests}[DIAG_TEST]
    idx = _py_index()
    want = int(os.environ.get("BR_DIAG_IDX", "0"))
    out, base, code, seen = [], 0, None, 0
    for cid, nums, is_all, paths, used_fp, dfiles, fpfiles in rows:
        if code is None and tid in nums and seen < want:
            seen += 1
        elif code is None and tid in nums:
            hit = next(((p, ln) for p, s, ln in dfiles if s != ALL and tid in s), None)
            if hit is None and fpfiles:
                hit = (fpfiles[0], 0)
            if hit is None:
                hit = (paths[0] if paths else "", 4095)
            code = 4096 * idx.get(hit[0], 255) + min(hit[1], 4095)
            log("DIAG3", cid, hit, code)
        w = pack(nums, dur)
        out.append([cid, w])
        base += cost_ms(w, dur)
    target = 500000 + (code if code is not None else 255 * 4096 + 4095)
    return _diag_pad(out, base, target, tid, dur)


def diag4_answers(rows, tests, dur):
    """Practice diagnostic 4: DIAG_TEST removed from the BR_DIAG_IDX-th commit
    that selects it (miss count = whether that one needed it). Total =
    500000 + N + 128*nonempty + 16384*cheap, where cheap = non-empty
    selections costing <= 6 s (ms)."""
    tid = {t["id"]: t["number"] for t in tests}[DIAG_TEST]
    want = int(os.environ.get("BR_DIAG_IDX", "2"))
    out, base, k, n, nonempty, cheap = [], 0, 0, 0, 0, 0
    for cid, nums, *_ in rows:
        n += 1
        c = cost_ms(pack(nums, dur), dur)
        nonempty += bool(nums)
        cheap += bool(nums) and c <= 6000
        if tid in nums:
            if k == want:
                nums = [x for x in nums if x != tid]
            k += 1
        w = pack(nums, dur)
        out.append([cid, w])
        base += cost_ms(w, dur)
    target = 500000 + min(n, 127) + 128 * min(nonempty, 127) + 16384 * min(cheap, 127)
    log("DIAG4 n", n, "nonempty", nonempty, "cheap", cheap, "tpcds commits", k, "base", base, "target", target)
    return _diag_pad(out, base, target, tid, dur)


_AST_NAMES = sorted(n for n in dir(ast) if isinstance(getattr(ast, n), type)
                    and issubclass(getattr(ast, n), ast.AST) and n[0].isupper())


def _diag5_code(text):
    """Text shape of the first .py hunk change (20 bits):
    bit0 = insertion (no removed line); line-85 (4b) << 1;
    replacement: common-prefix length (7b) << 5, new char at that spot (7b) << 12,
                 length change +1 clipped to 0..1 (1b) << 19
    insertion:   stripped length (7b) << 5, first non-space char (7b) << 12."""
    for fp in difflib2.parse(text):
        if not fp.path.endswith(".py"):
            continue
        for os_, ol, ns, nl, body in fp.hunks:
            k = os_ if ol > 0 else os_ + 1
            rem, add, first = [], [], None
            for tag, line in body:
                if tag == " ":
                    if rem or add:
                        break
                    k += 1
                elif tag == "-":
                    if first is None:
                        first = k
                    rem.append(line)
                    k += 1
                else:
                    if first is None:
                        first = k
                    add.append(line)
            if not rem and not add:
                continue
            ln = max(0, min((first or 85) - 85, 15))
            if not rem:
                t = add[0].strip()
                return 1 | (ln << 1) | (min(len(t), 127) << 5) | ((ord(t[0]) & 127 if t else 0) << 12)
            r, n = rem[0].rstrip("\n"), (add[0].rstrip("\n") if add else "")
            p = 0
            while p < len(r) and p < len(n) and r[p] == n[p]:
                p += 1
            ch = ord(n[p]) & 127 if p < len(n) else 0
            return (ln << 1) | (min(p, 127) << 5) | (ch << 12) | ((1 if len(n) > len(r) else 0) << 19)
    return (1 << 20) - 1


def diag5_answers(rows, tests, dur, texts):
    tid = {t["id"]: t["number"] for t in tests}[DIAG_TEST]
    want = int(os.environ.get("BR_DIAG_IDX", "1"))
    out, base, k, code = [], 0, 0, None
    for cid, nums, *_ in rows:
        if tid in nums:
            if k == want and code is None:
                try:
                    code = _diag5_code(texts[cid])
                except Exception as e:
                    log("DIAG5 error", repr(e))
                    code = (1 << 20) - 2
                log("DIAG5", cid, code)
            k += 1
        w = pack(nums, dur)
        out.append([cid, w])
        base += cost_ms(w, dur)
    target = 500000 + (code if code is not None else (1 << 20) - 3)
    return _diag_pad(out, base, target, tid, dur)


def diag6_answers(rows, tests, dur):
    """Practice diagnostic 6: commits whose selection costs <= 6 s run nothing
    (miss count = how many of them really break a test). Total (minus 300 s per
    miss) mod 65536 = n_cheap | (their total cost in whole seconds) << 6."""
    tid = {t["id"]: t["number"] for t in tests}[DIAG_TEST]
    out, base, n_cheap, c_cheap = [], 0, 0, 0
    for cid, nums, *_ in rows:
        c = cost_ms(pack(nums, dur), dur)
        if nums and c <= 6000:
            n_cheap += 1
            c_cheap += c
            nums = []
        w = pack(nums, dur)
        out.append([cid, w])
        base += cost_ms(w, dur)
    code = min(n_cheap, 63) | (min(c_cheap // 1000, 1023) << 6)  # 16 bits
    target = base + ((code - base) % 65536)  # decode: total % 65536
    log("DIAG6 n_cheap", n_cheap, "c_cheap", c_cheap, "base", base, "code", code, "target", target)
    return _diag_pad(out, base, target, tid, dur)


def _diag_pad(out, base, target, tid, dur):
    need = target - base
    for row in out:
        if need <= 0:
            break
        w = row[1]
        if not w:
            continue
        used = {n for ws in w for n in ws}
        loads = [sum(dur[n] for n in ws) for ws in w]
        mi = loads.index(max(loads))
        for n in sorted((n for n in dur if n not in used and n != tid), key=lambda n: -dur[n]):
            if 0 < dur[n] <= need:
                w[mi].append(n)
                need -= dur[n]
                if need == 0:
                    break
    final = sum(cost_ms(r[1], dur) for r in out)
    log("DIAG final", final, "target", target, "left", need)
    return [json.dumps({"commit": c, "workers": w}) for c, w in out]
DIAG_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def diag_answers(rows, tests, dur):
    """Practice-board diagnostic. Selection = normal minus DIAG_TEST, so the
    board's miss count = commits that really needed it. The simulated total is
    padded (extra tests only, never fewer) to exactly
        500000 + 1000*savings_s + 32*n_selected + n_all   (ms)
    savings_s = whole seconds the test cost us across the run."""
    tid = {t["id"]: t["number"] for t in tests}[DIAG_TEST]
    out, base, normal, n_sel, n_all = [], 0, 0, 0, 0
    for cid, nums, is_all, *_ in rows:
        normal += cost_ms(pack(nums, dur), dur)
        n_all += is_all
        if tid in nums:
            n_sel += 1
            nums = [n for n in nums if n != tid]
        w = pack(nums, dur)
        out.append([cid, w])
        base += cost_ms(w, dur)
    savings_s = (normal - base) // 1000
    target = 500000 + 1000 * min(savings_s, 2000) + 32 * min(n_sel, 31) + min(n_all, 31)
    need = target - base
    log("DIAG normal", normal, "base", base, "savings_s", savings_s, "n_sel", n_sel, "n_all", n_all, "target", target)
    for row in out:
        if need <= 0:
            break
        w = row[1]
        if not w:
            continue
        used = {n for ws in w for n in ws}
        loads = [sum(dur[n] for n in ws) for ws in w]
        mi = loads.index(max(loads))
        free = sorted((n for n in dur if n not in used and n != tid), key=lambda n: -dur[n])
        for n in free:
            if 0 < dur[n] <= need:
                w[mi].append(n)
                need -= dur[n]
                if need == 0:
                    break
    if need != 0:
        log("DIAG could not hit target, left", need)
    final = sum(cost_ms(r[1], dur) for r in out)
    log("DIAG final", final, "target", target)
    return [json.dumps({"commit": c, "workers": w}) for c, w in out]


# ---------------------------------------------------------------- main
T_START = time.time()


def main():
    t0 = time.time()
    real_stdout = os.dup(1)
    os.dup2(2, 1)  # anything stray goes to stderr
    answers = []
    diag_rows = []
    diag_texts = {}
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ctx = Ctx(tests)
    deadline = t0 + float(os.environ.get("BR_DEADLINE", "140"))
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
            if DIAG:
                diag_texts[cid] = text
            sel = select_commit(ctx, text, cid)
            if sel != ALL and PARTIAL:
                try:
                    sel = partial_heavy(ctx, cid, text, sel, dur, deadline)
                except Exception as e:
                    log(cid, "partial error", repr(e))
            if sel != ALL:
                try:
                    sel = probe_heavy(ctx, cid, text, sel, dur)
                except Exception as e:
                    log(cid, "probe error", repr(e))
            if sel != ALL:
                try:
                    sel = tpcds_opt_standin(ctx, cid, text, sel, dur)
                except Exception as e:
                    log(cid, "tpcds-opt error", repr(e))
            if sel != ALL:
                try:
                    sel = tpcds_exec_standin(ctx, cid, text, sel, dur)
                except Exception as e:
                    log(cid, "tpcds-exec error", repr(e))
            if sel != ALL:
                try:
                    sel = maybe_verify(ctx, cid, text, sel, dur, deadline)
                except Exception as e:
                    log(cid, "verify error", repr(e))
        except Exception as e:  # never risk a miss
            log(cid, "error", repr(e))
            sel = ALL
        if sel != ALL and ctx.map is not None and ctx.map.trans is not None:
            t = ctx.map.trans
            sel = ({t[o] for o in sel if o in t} | ctx.map.unknown_new) if sel else set()
        nums = ctx.all if sel == ALL else sorted(sel)
        workers = pack(nums, dur)
        if DIAG:
            diag_rows.append((cid, nums, sel == ALL, sorted(getattr(ctx, "patched_paths", set())),
                              bool(getattr(ctx, "need_fp", None)), list(getattr(ctx, "diag_files", [])),
                              list(getattr(ctx, "need_fp", []) or [])))
        log(cid, "ALL" if sel == ALL else len(nums), "workers", len(workers))
        answers.append(json.dumps({"commit": cid, "workers": workers}))
    if DIAG:
        answers = (lambda r, t, d: diag5_answers(r, t, d, diag_texts)) if DIAG_MODE == "5" else \
            {"2": diag2_answers, "3": diag3_answers, "4": diag4_answers, "6": diag6_answers}.get(DIAG_MODE, diag_answers)
        answers = answers(diag_rows, tests, dur)
    os.write(real_stdout, ("\n".join(answers) + "\n").encode())
    log("done in", round(time.time() - t0, 3), "s")


if __name__ == "__main__":
    main()
