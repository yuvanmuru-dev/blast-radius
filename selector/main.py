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
    if path.endswith(".py"):
        if ast_equal(old, new):
            return set()  # comment / whitespace only (docstrings are AST)
        if ctx.map is None:
            return ALL
        b = rules.select_py(ctx.map, path, old, new, fp)
        if b == ALL:
            return ALL
        bits, need_fp = b
        if need_fp:
            if not path.startswith("sqlglot/"):
                # fingerprint covers sqlglot modules only: be conservative elsewhere
                return rules.bits_to_set(bits | ctx.map.file_any(path) | ctx.map.always)
            ctx.need_fp.append(path)
        return rules.bits_to_set(bits)
    if ctx.map is None:
        return ALL
    b = ctx.map.opened(path)
    if b:
        b |= ctx.map.always
    return rules.bits_to_set(b)


def select_commit(ctx, text, cid="x"):
    files = difflib2.parse(text)
    if not files:
        return ALL
    ctx.need_fp = []
    out = set()
    for fp in files:
        s = select_file(ctx, fp)
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
FP_BASE = Path(os.environ.get("BR_FPBASE", str(HERE.parent / "maps" / "fp_base.json")))
_FPB = [None]
_src_index = [None]


def _fp_base():
    if _FPB[0] is None:
        _FPB[0] = json.loads(FP_BASE.read_text())
    return _FPB[0]


def source_lines_with(tokens):
    """{relfile: [line numbers]} of lines in sqlglot/ and tests/ mentioning any token."""
    if _src_index[0] is None:
        idx = {}
        for base in ("sqlglot", "tests"):
            for d, dirs, files in os.walk(SRC / base):
                dirs[:] = [x for x in dirs if x != "__pycache__" and x != "fixtures"]
                for f in files:
                    if f.endswith(".py"):
                        p = Path(d) / f
                        idx[str(p.relative_to(SRC))] = p.read_text(encoding="utf-8").split("\n")
        _src_index[0] = idx
    import re
    groups = [t for t in tokens if t]
    flat = sorted({p for t in groups for p in t})
    if not flat:
        return {}
    pat = re.compile(r"\b(" + "|".join(re.escape(t) for t in flat) + r")\b")
    out = {}
    for f, lines in _src_index[0].items():
        hits = []
        for i, l in enumerate(lines):
            found = set(pat.findall(l))
            if found and any(all(p in found for p in g) for g in groups):
                hits.append(i + 1)
        if hits:
            out[f] = hits
    return out


_IMPORTERS = [None]


def _modname(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def affected_modules(paths):
    """Changed modules plus every sqlglot module that imports them, transitively
    (static imports anywhere in the file, relative ones resolved)."""
    import ast as _ast
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
                for n in _ast.walk(tree):
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


def fingerprint_select(ctx, text, cid):
    """Class/module-level edits: diff the patched import-time state against the
    baseline and select tests that use any changed class or name."""
    M = ctx.map
    base = _fp_base()
    files = patched_files(text)
    mods = affected_modules(ctx.need_fp)
    ov = verify.build_overlay(SRC, "/tmp/ov", files)
    outp = "/tmp/fp_out.json"
    try:
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=str(ov), PYTHONDONTWRITEBYTECODE="1")
        import subprocess
        r = subprocess.run([sys.executable, str(HERE / "fp.py"), outp] + mods, cwd=str(ov), env=env,
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
    bfp = {k: v for k, v in full.items() if k.split(":", 1)[0] in modset}
    for k in nfp:
        if k in full:
            bfp[k] = full[k]
    by_owner = {}
    glob_tokens = set()
    for k in set(bfp) | set(nfp):
        a, b = bfp.get(k), nfp.get(k)
        if a == b:
            continue
        owner, _, kind = (a or b)
        if (a and b) and a[2] != b[2]:
            kind = "F"
        if owner:
            mod = k.split(":", 1)[0]
            attr = k.rsplit(".", 1)[-1]
            qual = owner[len(mod) + 1:] if owner.startswith(mod + ".") else owner.rsplit(".", 1)[-1]
            ent = by_owner.setdefault(owner, {"qual": qual, "attrs": set(), "all_values": True})
            ent["attrs"].add(attr)
            if kind != "V" or attr.startswith("__"):
                ent["all_values"] = False
        else:
            glob_tokens.add((k.split(":", 1)[1],))
    log(cid, "fp changed", {o: sorted(e["attrs"])[:4] for o, e in list(by_owner.items())[:4]}, sorted(glob_tokens)[:4])
    if len(by_owner) > 400:
        return ALL
    if glob_tokens:
        for f, lines in source_lines_with(glob_tokens).items():
            for l in lines:
                bits |= M.lines("f", f, l, l)
    for owner, ent in by_owner.items():
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
VERIFY = os.environ.get("BR_VERIFY", "1") == "1"


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
    est_cpu = 350 + 1.3 * sum(dur[n] for n in light)  # interpreter + imports + run
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


# ---------------------------------------------------------------- main
def main():
    t0 = time.time()
    real_stdout = os.dup(1)
    os.dup2(2, 1)  # anything stray goes to stderr
    answers = []
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ctx = Ctx(tests)
    deadline = t0 + float(os.environ.get("BR_DEADLINE", "140"))
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
            sel = select_commit(ctx, text, cid)
            if sel != ALL:
                try:
                    sel = maybe_verify(ctx, cid, text, sel, dur, deadline)
                except Exception as e:
                    log(cid, "verify error", repr(e))
        except Exception as e:  # never risk a miss
            log(cid, "error", repr(e))
            sel = ALL
        nums = ctx.all if sel == ALL else sorted(sel)
        workers = pack(nums, dur)
        log(cid, "ALL" if sel == ALL else len(nums), "workers", len(workers))
        answers.append(json.dumps({"commit": cid, "workers": workers}))
    os.write(real_stdout, ("\n".join(answers) + "\n").encode())
    log("done in", round(time.time() - t0, 3), "s")


if __name__ == "__main__":
    main()
