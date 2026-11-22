"""Patch -> candidate tests, using the per-test map. Any doubt -> ALL."""
import ast
import gzip
import json
import os

ALL = "ALL"
FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


class Map:
    def __init__(self, path, n, tests=()):
        with gzip.open(path, "rt") as g:
            d = json.load(g)
        ids = d.get("ids")
        cur = [t["id"] for t in tests] if tests else None
        self.trans = None
        if cur is not None and ids is not None and ids != cur:
            pos = {i: k for k, i in enumerate(cur)}
            self.trans = {k: pos[i] for k, i in enumerate(ids) if i in pos}
            self.unknown_new = set(range(len(cur))) - set(self.trans.values())
        elif d["n"] != n:
            raise ValueError("map/tests mismatch")
        self.n = n
        self.sets = [int(h, 16) for h in d["sets"]]
        self.m = {f: {int(l): i for l, i in v.items()} for f, v in d["m"].items()}
        self.f = {f: {int(l): i for l, i in v.items()} for f, v in d["f"].items()}
        self.o = d["o"]
        self.r = d.get("r", {})
        self.ka = d.get("ka", {})
        self.kw = d.get("kw", {})
        self.keys_ok = bool(d.get("keys_ok"))
        self.cmp = {}
        cp = os.path.join(os.path.dirname(path), "cmpprof.json.gz")
        if os.path.exists(cp):
            with gzip.open(cp, "rt") as g:
                self.cmp = {k: set(v.split(",")) for k, v in json.load(g).items()}
        # tests we cannot vouch for: included whenever a commit does anything
        self.always = 0
        self.by_file = {}
        for t in tests:
            self.by_file[t["file"]] = self.by_file.get(t["file"], 0) | (1 << t["number"])
        for num in d["unmapped"] + d["notok"] + d["subproc"]:
            self.always |= 1 << num
        self._file_any = {}

    span_log = None
    nonspan = False

    def lines(self, kind, f, lo, hi):
        if kind == "f" and self.span_log is not None:
            self.span_log.append((f, lo, hi))
        tab = (self.m if kind == "m" else self.f).get(f)
        bits = 0
        if not tab:
            return 0
        if hi - lo < 64:
            for l in range(lo, hi + 1):
                i = tab.get(l)
                if i is not None:
                    bits |= self.sets[i]
        else:
            for l, i in tab.items():
                if lo <= l <= hi:
                    bits |= self.sets[i]
        return bits

    def file_any(self, f):
        self.nonspan = True
        if f not in self._file_any:
            bits = 0
            for tab in (self.m.get(f, {}), self.f.get(f, {})):
                for i in set(tab.values()):
                    bits |= self.sets[i]
            bits |= self.sets[self.o[f]] if f in self.o else 0
            self._file_any[f] = bits
        return self._file_any[f]

    def file_tests(self, tf):
        return self.by_file.get(tf, 0)

    def key_reads(self, owner, attr, kds):
        """Tests that read any of the given entries (or the whole dict)."""
        self.nonspan = True
        ent = f"{owner}\t{attr}"
        bits = 0
        w = self.kw.get(ent)
        if w is not None:
            bits |= self.sets[w]
        tab = self.ka.get(ent, {})
        for kd in kds:
            i = tab.get(kd)
            if i is not None:
                bits |= self.sets[i]
        return bits

    def receivers(self, cname):
        self.nonspan = True
        i = self.r.get(cname)
        return self.sets[i] if i is not None else 0

    def opened(self, f):
        self.nonspan = True
        return self.sets[self.o[f]] if f in self.o else 0


def bits_to_set(b):
    out = set()
    i = 0
    while b:
        if b & 1:
            out.add(i)
        b >>= 1
        i += 1
    return out


# ------------------------------------------------------------ AST helpers
def func_index(tree):
    """qualname -> FunctionDef node (nested and methods included)."""
    out = {}

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, FUNC + (ast.ClassDef,)):
                q = prefix + ch.name
                if isinstance(ch, FUNC):
                    out.setdefault(q, []).append(ch)
                walk(ch, q + ".")
            else:
                walk(ch, prefix)

    walk(tree, "")
    return out


def innermost_func(tree, lines):
    """Innermost def whose *body* (excluding a leading docstring? no: including)
    contains every line in `lines`. Returns (qualname, node) or None."""
    best = None

    def walk(node, prefix):
        nonlocal best
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, FUNC + (ast.ClassDef,)):
                q = prefix + ch.name
                if isinstance(ch, FUNC) and ch.body:
                    lo, hi = ch.body[0].lineno, ch.end_lineno
                    # decorators / signature must be untouched
                    if all(lo <= l <= hi for l in lines):
                        best = (q, ch)
                walk(ch, q + ".")
            else:
                walk(ch, prefix)

    walk(tree, "")
    return best


def stored_names(fn):
    names, flags = set(), set()
    for node in ast.walk(fn):
        if node is not fn and isinstance(node, FUNC + (ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            flags.add(type(node).__name__ + ":" + ",".join(node.names))
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            flags.add("yield")
        elif isinstance(node, ast.Await):
            flags.add("await")
        elif isinstance(node, ast.arg):
            names.add("arg:" + node.arg)
    return names, flags


def sig_dump(fn):
    return ast.dump(ast.Module(body=[], type_ignores=[])) + ast.dump(fn.args) + "".join(
        ast.dump(d) for d in fn.decorator_list
    ) + (ast.dump(fn.returns) if fn.returns else "") + fn.name


def hunk_regions(fp):
    """Per hunk: (old changed lines incl. insertion anchors, new added lines)."""
    regions = []
    for os_, ol, ns, nl, body in fp.hunks:
        k = os_ if ol > 0 else os_ + 1
        j = ns if nl > 0 else ns + 1
        old_l, new_l, anch, rem = [], [], set(), []
        for tag, _ in body:
            if tag == " ":
                k += 1; j += 1
            elif tag == "-":
                old_l.append(k); rem.append(k); k += 1
            else:
                new_l.append(j); j += 1
                old_l.append(k - 1); old_l.append(k)  # anchors
                anch.add(k - 1); anch.add(k)
        regions.append((sorted(set(x for x in old_l if x > 0)), new_l, rem, len(new_l)))
    return regions


_FN_CACHE = {}


def _fn_nodes(tree):
    k = id(tree)
    ent = _FN_CACHE.get(k)
    if ent is None or ent[0] is not tree:
        ns = [n for n in ast.walk(tree) if isinstance(n, FUNC + (ast.Lambda,))]
        ns.sort(key=lambda n: (n.lineno, n.col_offset))
        ent = (tree, ns)
        if len(_FN_CACHE) > 8:
            _FN_CACHE.clear()
        _FN_CACHE[k] = ent
    return ent[1]


def same_tree(a, b, skip=None):
    """Structural AST equality (like comparing ast.dump without positions).
    skip: {id(old_node): new_node} pairs whose bodies are ignored."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, ast.AST):
            sk = skip is not None and id(x) in skip
            if sk and skip[id(x)] is not y:
                return False
            for f in x._fields:
                if sk and f == "body":
                    continue
                stack.append((getattr(x, f, None), getattr(y, f, None)))
        elif isinstance(x, list):
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif x != y:
            return False
    return True


def _nodes_containing(tree, lines, kinds):
    """All nodes of `kinds` whose body span contains every line, outermost first."""
    out = []
    for node in _fn_nodes(tree):
        if isinstance(node, kinds):
            if isinstance(node, ast.Lambda):
                lo, hi = node.body.lineno, node.body.end_lineno
            else:
                if not node.body:
                    continue
                lo, hi = node.body[0].lineno, node.end_lineno
            if all(lo <= l <= hi for l in lines):
                out.append(node)
    out.sort(key=lambda n: (n.lineno, -n.end_lineno))
    return out


def _ordered(tree, key):
    return [n for n in _fn_nodes(tree) if _key(n) == key]


def _match_node(ot, nt, onode):
    k = _key(onode)
    a, b = _ordered(ot, k), _ordered(nt, k)
    if len(a) != len(b):
        return None
    return b[a.index(onode)]


def _match_back(ot, nt, nnode):
    k = _key(nnode)
    a, b = _ordered(ot, k), _ordered(nt, k)
    if len(a) != len(b):
        return None
    for x, y in zip(a, b):
        if y is nnode:
            return x
    return None


def _blank(node):
    if isinstance(node, ast.Lambda):
        node.body = ast.Constant(value="__BR__")
    else:
        node.body = [ast.Pass()]


def _key(node):
    return (type(node).__name__, getattr(node, "name", "<lambda>"), ast.dump(node.args))


def select_py(M, path, old, new, fp, ot=None, nt=None):
    if ot is None:
        try:
            ot = ast.parse(old)
        except SyntaxError:
            return ALL
    if nt is None:
        try:
            nt = ast.parse(new)
        except SyntaxError:
            return (M.file_any(path) | M.always, False)  # import breaks for its importers
    if same_tree(ot, nt):
        return (0, False)
    old_text = old.split("\n")
    new_text = new.split("\n")
    plan = []  # (old_node, spans or None)
    need_fp = False
    fallback_bits = 0
    regions = hunk_regions(fp)
    n_regions = len(regions)
    for old_l, new_l, rem, n_added in regions:
        code_rem = [l for l in rem if not _is_trivia(old_text[l - 1])]
        code_add = [l for l in new_l if not _is_trivia(new_text[l - 1])]
        if not code_rem and not code_add:
            continue  # comments / blank lines only
        anchor_lines = code_rem if code_rem else [l for l in old_l]
        olds = _nodes_containing(ot, anchor_lines, FUNC + (ast.Lambda,))
        news = _nodes_containing(nt, code_add, FUNC + (ast.Lambda,)) if code_add else []
        if not code_rem and news:
            # pure insertion: locate it in the new tree (the old-side anchors can
            # straddle a function boundary), then map back to the old function
            olds = [o for o in (_match_back(ot, nt, x) for x in news) if o is not None]
            if len(olds) != len(news):
                # cannot map back: whole enclosing outer function in the new tree
                outer = news[0]
                ob = _match_back(ot, nt, outer)
                if ob is not None:
                    lo = ob.lineno if isinstance(ob, ast.Lambda) else ob.body[0].lineno
                    fallback_bits |= M.lines("f", path, ob.lineno, ob.end_lineno)
                need_fp = True
                continue
        elif news and not olds:
            # code removed outside any function but added inside one (or vice versa)
            need_fp = True
            for x in news:
                ob = _match_back(ot, nt, x)
                if ob is not None:
                    fallback_bits |= M.lines("f", path, ob.lineno, ob.end_lineno)
            continue
        if not olds:
            need_fp = True  # class / module level: import-time fingerprint decides
            continue
        if code_rem and code_add and news:
            # removed lines in one function, added lines in another -> both
            ob = _match_back(ot, nt, news[-1])
            if ob is not None and ob is not olds[-1] and not any(ob is o for o in olds):
                fallback_bits |= M.lines("f", path, ob.lineno, ob.end_lineno)
                need_fp = True
        onode = olds[-1]
        nnode = _match_node(ot, nt, onode)
        if nnode is None or (code_add and (not news or news[-1] is not nnode)):
            # the edit is inside a function body but reshapes a nested function or
            # lambda: everything that ran the outermost enclosing function, plus the
            # import-time fingerprint for anything else
            outer = olds[0]
            lo = outer.lineno if isinstance(outer, ast.Lambda) else outer.body[0].lineno
            fallback_bits |= M.lines("f", path, lo, outer.end_lineno)
            if outer.lineno != lo:
                fallback_bits |= M.lines("f", path, outer.lineno, lo)
            need_fp = True
            continue
        spans = None
        if not isinstance(onode, ast.Lambda) and nnode is not None and stored_names(onode) == stored_names(nnode):
            if _expr_only_change(onode, nnode):
                spans = _spans_for(onode, rem, code_add, old_text)
            elif n_regions == 1 and code_add and not code_rem:
                spans = _insertion_spans(onode, old_l, new_l, new_text, old_text)
            elif n_regions == 1 and code_rem and not code_add:
                spans = _deletion_spans(onode, code_rem, old_text)
        plan.append((onode, nnode, spans))
    # provably equivalent operator swaps (per-site value profile of the whole suite)
    if M.cmp and plan:
        keep = []
        for onode, nnode, spans in plan:
            if nnode is not None and equivalent_swap(M, path, onode, nnode):
                continue
            keep.append((onode, nnode, spans))
        if not keep and not need_fp:
            # every hunk was an equivalent swap: verify nothing else changed
            if _only_swaps_differ(ot, nt, M, path):
                if path.startswith("sqlglot/typing/"):
                    return (M.file_tests("tests/test_typing.py") | M.always, False)
                return (0, False)
        plan = keep if keep else plan
    if not plan:
        return (fallback_bits | (M.always if fallback_bits else 0), need_fp)
    # rigor: outside the changed bodies, old and new must be identical
    skip = {id(onode): nnode for onode, nnode, _ in plan if nnode is not None}
    if len(skip) != len({id(o) for o, _, _ in plan}) or not same_tree(ot, nt, skip):
        need_fp = True  # something outside the matched bodies changed too
    bits = 0
    for onode, nnode, spans in plan:
        if spans is None:
            if isinstance(onode, ast.Lambda):
                lo, hi = onode.lineno, onode.end_lineno
            else:
                lo, hi = onode.body[0].lineno, onode.end_lineno
            bits |= M.lines("f", path, lo, hi)
        else:
            for lo, hi in spans:
                bits |= M.lines("f", path, lo, hi)
    if path.startswith("sqlglot/typing/"):
        bits |= M.file_tests("tests/test_typing.py")
    bits |= fallback_bits
    if bits:
        bits |= M.always
    return (bits, need_fp)


def fp_hunk_removed(fp, old_l):
    return None


def _is_trivia(txt):
    t = txt.strip()
    return t == "" or t.startswith("#")


def stmt_spans(fn, old_l, new_l, old_text, new_text, _):
    """Statement-level spans in the old function for one hunk, or None for
    function-level. old_l mixes removed lines and insertion anchors, so we
    recompute: removed = old_l lines whose text is not kept... we only get the
    combined list; treat a hunk with added code lines and no removable statement
    as an insertion (function-level)."""
    # removed lines are those in old_l that are non-trivia; anchors may be trivia
    code_added = [l for l in new_l if not _is_trivia(new_text[l - 1])]
    return _spans_for(fn, old_l, code_added, old_text)


def _innermost_stmt(fn, line):
    best = None
    def walk(stmts):
        nonlocal best
        for st in stmts:
            if st.lineno <= line <= st.end_lineno:
                best = st
                for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    sub = getattr(st, field, None)
                    if sub:
                        walk(sub)
                return
    walk(fn.body)
    return best


def _header_span(st):
    kids = []
    for field in ("body", "orelse", "finalbody", "handlers", "cases"):
        sub = getattr(st, field, None)
        if sub:
            kids.extend(sub)
    if not kids:
        return None
    first = min(k.lineno for k in kids)
    return st.lineno, first - 1


COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
            ast.Match) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())


def _spans_for(fn, old_l, code_added, old_text):
    removed_code = [l for l in old_l if 0 < l <= len(old_text) and not _is_trivia(old_text[l - 1])]
    if not removed_code:
        if not code_added:
            return []  # comment / blank only hunk
        return None  # pure insertion -> function level
    if len(code_added) > len(removed_code):
        return None  # net insertion of code -> function level
    spans = []
    for l in removed_code:
        st = _innermost_stmt(fn, l)
        if st is None:
            return None
        if isinstance(st, FUNC + (ast.ClassDef,)):
            return None  # nested def/class header
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            return None  # docstring
        if isinstance(st, COMPOUND):
            hs = _header_span(st)
            if hs is None or not (hs[0] <= l <= hs[1]):
                # e.g. "else:" / "finally:" / "except X:" lines
                if isinstance(st, ast.Try):
                    return None
                return None
            spans.append(hs)
        else:
            spans.append((st.lineno, st.end_lineno))
    return spans


EQ_GROUPS = [{ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq}]
ORD_GROUPS = [{ast.Lt, ast.LtE}, {ast.Gt, ast.GtE}]


def _diff_points(a, b, out):
    """Collect (a, b) node pairs where the trees first differ."""
    if type(a) is not type(b):
        out.append((a, b)); return
    if isinstance(a, ast.Compare) and len(a.ops) == len(b.ops) and any(
            type(x) is not type(y) for x, y in zip(a.ops, b.ops)):
        if ast.dump(a.left) == ast.dump(b.left) and all(
                ast.dump(x) == ast.dump(y) for x, y in zip(a.comparators, b.comparators)):
            out.append((a, b)); return
        out.append((None, None)); return
    if isinstance(a, ast.AST):
        for f in a._fields:
            va, vb = getattr(a, f, None), getattr(b, f, None)
            if isinstance(va, list) or isinstance(vb, list):
                if not isinstance(va, list) or not isinstance(vb, list) or len(va) != len(vb):
                    out.append((a, b)); return
                for x, y in zip(va, vb):
                    _diff_points(x, y, out)
            elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
                _diff_points(va, vb, out)
            elif va != vb:
                out.append((a, b)); return
    elif a != b:
        out.append((a, b))


def _cmp_pair_ok(M, path, a, b):
    if not (isinstance(a, ast.Compare) and isinstance(b, ast.Compare)) or len(a.ops) != 1:
        return False
    flags = M.cmp.get(f"{path}:{a.lineno}:{a.col_offset}")
    if flags is None or "eqerr" in flags:
        return False  # never evaluated in the profile (the line map decides) or unsafe
    pair = {type(a.ops[0]), type(b.ops[0])}
    if any(pair <= g for g in EQ_GROUPS):
        return "isne" not in flags and "eqnonbool" not in flags
    if any(pair <= g for g in ORD_GROUPS):
        return "equal" not in flags
    return False


def equivalent_swap(M, path, onode, nnode):
    pts = []
    _diff_points(onode, nnode, pts)
    return len(pts) == 1 and _cmp_pair_ok(M, path, *pts[0])


def _only_swaps_differ(ot, nt, M, path):
    pts = []
    _diff_points(ot, nt, pts)
    return all(_cmp_pair_ok(M, path, a, b) for a, b in pts)


def _expr_only_change(onode, nnode):
    """True when old and new function bodies have the same statement structure
    and differ only inside expressions (so control flow reaches the same
    statements). Otherwise statement-level selection is unsound."""
    pts = []
    _diff_points_body(onode, nnode, pts)
    for a, b in pts:
        if a is None or b is None:
            return False
        if isinstance(a, ast.stmt) or isinstance(b, ast.stmt):
            return False
        if isinstance(a, (ast.excepthandler, ast.match_case, ast.withitem)) or isinstance(
                b, (ast.excepthandler, ast.match_case, ast.withitem)):
            return False
    return True


def _diff_points_body(onode, nnode, out):
    if len(onode.body) != len(nnode.body):
        out.append((onode, nnode))
        return
    for x, y in zip(onode.body, nnode.body):
        _diff_points(x, y, out)


def _strip_inert(tree):
    """Copy of tree without module/class-level docstrings, TYPE_CHECKING blocks
    and annotation-only differences (none of which run at import)."""
    import copy
    t = copy.deepcopy(tree)

    def is_tc(test):
        return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")

    def clean(body):
        out = []
        for st in body:
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                continue
            if isinstance(st, ast.If) and is_tc(st.test):
                st.body = [ast.Pass()]
            if isinstance(st, ast.AnnAssign):
                if st.value is None:
                    continue
                st.annotation = ast.Constant(None)
            if isinstance(st, ast.ClassDef):
                st.body = clean(st.body) or [ast.Pass()]
            if isinstance(st, FUNC):
                if st.body and isinstance(st.body[0], ast.Expr) and isinstance(getattr(st.body[0], "value", None), ast.Constant) \
                        and isinstance(st.body[0].value.value, str):
                    st.body = st.body[1:] or [ast.Pass()]
                st.returns = None
                for a in st.args.args + st.args.kwonlyargs + st.args.posonlyargs:
                    a.annotation = None
            out.append(st)
        return out

    t.body = clean(t.body)
    return t


def inert_change(old, new):
    try:
        return same_tree(_strip_inert(ast.parse(old)), _strip_inert(ast.parse(new)))
    except SyntaxError:
        return False


def _stmts_in(fn):
    out = []
    def walk(stmts):
        for st in stmts:
            out.append(st)
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                sub = getattr(st, field, None)
                if sub and not isinstance(st, FUNC + (ast.ClassDef,)):
                    walk(sub)
    walk(fn.body)
    return out


def _stmt_span(st):
    if isinstance(st, COMPOUND):
        hs = _header_span(st)
        return hs if hs else (st.lineno, st.end_lineno)
    return (st.lineno, st.end_lineno)


def _insertion_spans(fn, old_l, new_l, new_text, old_text):
    """Pure insertion of whole statements inside fn: the new code runs exactly
    when control reaches the insertion point, i.e. for tests that ran the old
    statement right after it (or, at the end of a block, the one right before)."""
    code_new = [l for l in new_l if not _is_trivia(new_text[l - 1])]
    if not code_new:
        return []
    indent = len(new_text[code_new[0] - 1]) - len(new_text[code_new[0] - 1].lstrip())
    # old line numbers around the insertion point (anchors are k-1, k)
    if not old_l:
        return None
    nxt = max(old_l)
    prv = min(old_l)
    stmts = _stmts_in(fn)
    # skip blank / comment lines to the next real code line
    n = nxt
    while n <= len(old_text) and _is_trivia(old_text[n - 1]):
        n += 1
    for st in stmts:
        if st.lineno == n and st.col_offset == indent:
            return [_stmt_span(st)]
    p = prv
    while p >= 1 and _is_trivia(old_text[p - 1]):
        p -= 1
    for st in stmts:
        if st.end_lineno == p and st.col_offset == indent:
            if isinstance(st, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                return []  # unreachable after it
            return [_stmt_span(st)]
    return None


def _deletion_spans(fn, code_rem, old_text):
    """Pure deletion of whole statements: affects tests that ran them."""
    rem = set(code_rem)
    out = []
    for st in _stmts_in(fn):
        lines = {l for l in range(st.lineno, st.end_lineno + 1) if not _is_trivia(old_text[l - 1])}
        if lines and lines <= rem:
            # outermost fully-deleted statements only
            if not any(o[0] <= st.lineno and st.end_lineno <= o[1] for o in out):
                out.append((st.lineno, st.end_lineno))
    covered = set()
    for lo, hi in out:
        covered |= {l for l in range(lo, hi + 1)}
    if not out or not rem <= covered:
        return None
    return out
