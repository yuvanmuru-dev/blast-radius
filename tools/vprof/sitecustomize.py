"""Per-test value profiler for operator swaps, auto-loaded (sitecustomize) by
every Python process started with this directory on PYTHONPATH, including
spawned pool workers (they inherit PYTEST_CURRENT_TEST from the test that
started them).

Rewrites sqlglot/ source at import time:
  * every single-operator comparison (is, is not, ==, !=, <, <=, >, >=) calls
    __br_cmp__, which returns exactly the original result and notes whether
    `is` and `==` disagreed (isne), == was not a bool (eqnonbool), == raised
    (eqerr), or the operands of an ordering were equal (equal);
  * every and/or whose value is only used for its truth (if/while/assert/
    ternary tests, comprehension filters, `not` operands, nested operands) and
    whose operands are side-effect free calls __br_bool__, which evaluates
    exactly like the original (short-circuit included) and then, in a guarded
    second pass, notes whether the operands' truth values differed (boolne) or
    whether evaluating a skipped operand raised (boolerr).
Every note is keyed by the current test: "<id>" during the call phase,
"<file>::<Class>" during setup/teardown (applies to the whole class) and "*"
outside any test (applies to everything).
"""
import ast
import builtins
import importlib.abc
import os
import sys

_ROOT = os.environ.get("BRPROF_ROOT")
_OUT = os.environ.get("BRPROF_OUT", "/tmp/vprof")
if _ROOT:
    _ROOT = os.path.realpath(_ROOT) + os.sep
    os.makedirs(_OUT, exist_ok=True)
    _fh = [None]
    _seen = set()
    def _key_of(cur):
        if not cur:
            return "*"
        tid, _, phase = cur.rpartition(" ")
        if phase == "(call)":
            return tid
        parts = tid.split("::")
        return "::".join(parts[:2]) if len(parts) > 2 else parts[0]

    # the pytest plugin (brvprof_plugin) keeps this current in the test process;
    # worker processes inherit the test's PYTEST_CURRENT_TEST and keep it
    builtins.__br_key__ = _key_of(os.environ.get("PYTEST_CURRENT_TEST"))
    builtins.__br_key_of__ = _key_of

    def _key():
        return builtins.__br_key__

    def _note(site, flag):
        k = (site, flag, _key())
        if k in _seen:
            return
        _seen.add(k)
        if _fh[0] is None:
            _fh[0] = open(os.path.join(_OUT, f"{os.getpid()}.txt"), "a", buffering=1)
        _fh[0].write("\t".join(k) + "\n")

    _done = {}

    def _flagged(site, flag):
        return (site, flag, builtins.__br_key__) in _seen

    def __br_cmp__(site, op, l, r):
        if op == 0:
            res = l is r
        elif op == 1:
            res = l is not r
        elif op == 2:
            res = l == r
        elif op == 3:
            res = l != r
        elif op == 4:
            res = l < r
        elif op == 5:
            res = l <= r
        elif op == 6:
            res = l > r
        else:
            res = l >= r
        k = builtins.__br_key__
        if op <= 3:
            if (site, "isne", k) in _seen and (site, "eqnonbool", k) in _seen:
                return res
        elif (site, "equal", k) in _seen:
            return res
        if (site, "eqerr", k) in _seen:
            return res
        try:
            eq = l == r
            eqb = bool(eq)
        except BaseException:  # noqa: BLE001
            _note(site, "eqerr")
            return res
        if op <= 3:
            if (l is r) != eqb:
                _note(site, "isne")
            if type(eq) is not bool:
                _note(site, "eqnonbool")
        elif eqb:
            _note(site, "equal")
        return res

    def __br_bool__(site, is_and, thunks):
        # original semantics
        res = None
        n_eval = 0
        for t in thunks:
            res = t()
            n_eval += 1
            if is_and and not res:
                break
            if not is_and and res:
                break
        k = builtins.__br_key__
        if (site, "boolne", k) in _seen or (site, "boolerr", k) in _seen:
            return res
        try:
            # operands are side-effect free: re-evaluate all of them
            if len({bool(t()) for t in thunks}) > 1:
                _note(site, "boolne")
        except BaseException:  # noqa: BLE001
            _note(site, "boolerr")
        return res

    builtins.__br_cmp__ = __br_cmp__
    builtins.__br_bool__ = __br_bool__
    with open(__file__, "rb") as _sf:
        import hashlib as _hl
        _SELF_HASH = _hl.sha1(_sf.read()).hexdigest().encode()
    OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3, ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}
    PURE_CALLS = {"isinstance", "len", "bool", "type", "callable", "hasattr"}
    PURE_METHODS = {"get", "startswith", "endswith", "isdigit", "upper", "lower", "find", "is_type",
                    "args", "text", "meta_get"}

    def _pure(n):
        if isinstance(n, (ast.Name, ast.Constant)):
            return True
        if isinstance(n, ast.Attribute):
            return _pure(n.value)
        if isinstance(n, ast.Subscript):
            return _pure(n.value) and _pure(n.slice)
        if isinstance(n, ast.UnaryOp):
            return _pure(n.operand)
        if isinstance(n, ast.Compare):
            return _pure(n.left) and all(_pure(c) for c in n.comparators)
        if isinstance(n, ast.BoolOp):
            return all(_pure(v) for v in n.values)
        if isinstance(n, (ast.Tuple, ast.List)):
            return all(_pure(e) for e in n.elts)
        if isinstance(n, ast.Call):
            f = n.func
            ok = (isinstance(f, ast.Name) and f.id in PURE_CALLS) or (
                isinstance(f, ast.Attribute) and f.attr in PURE_METHODS and _pure(f.value))
            return ok and all(_pure(a) for a in n.args) and all(_pure(k.value) for k in n.keywords)
        return False

    class _T(ast.NodeTransformer):
        def __init__(self, rel):
            self.rel = rel
            self.truth = set()
            self.fdepth = 0
            self.inst = []

        def _mark(self, node):
            if node is not None:
                self.truth.add(id(node))

        def visit_FunctionDef(self, node):
            self.fdepth += 1
            self.generic_visit(node)
            self.fdepth -= 1
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            saved, self.fdepth = self.fdepth, 0  # class scope: lambdas cannot see its names
            self.generic_visit(node)
            self.fdepth = saved
            return node

        def visit_Lambda(self, node):
            self.fdepth += 1
            self.generic_visit(node)
            self.fdepth -= 1
            return node

        def visit_If(self, node):
            self._mark(node.test)
            self.generic_visit(node)
            return node

        visit_While = visit_If

        def visit_IfExp(self, node):
            self._mark(node.test)
            self.generic_visit(node)
            return node

        def visit_Assert(self, node):
            self._mark(node.test)
            self.generic_visit(node)
            return node

        def visit_comprehension(self, node):
            for c in node.ifs:
                self._mark(c)
            self.generic_visit(node)
            return node

        def visit_UnaryOp(self, node):
            if isinstance(node.op, ast.Not):
                self._mark(node.operand)
            self.generic_visit(node)
            return node

        def visit_BoolOp(self, node):
            truth = id(node) in self.truth
            if truth:
                for v in node.values:
                    self._mark(v)
            site = f"{self.rel}:{node.lineno}:{node.col_offset}"
            ok = truth and self.fdepth > 0 and all(_pure(v) for v in node.values)
            self.generic_visit(node)
            if not ok:
                return node
            self.inst.append(site)
            thunks = ast.List(elts=[ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                                                  kw_defaults=[], defaults=[]), body=v)
                                    for v in node.values], ctx=ast.Load())
            call = ast.Call(func=ast.Name(id="__br_bool__", ctx=ast.Load()),
                            args=[ast.Constant(site), ast.Constant(isinstance(node.op, ast.And)), thunks],
                            keywords=[])
            return ast.copy_location(call, node)

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
                return node
            site = f"{self.rel}:{node.lineno}:{node.col_offset}"
            call = ast.Call(
                func=ast.Name(id="__br_cmp__", ctx=ast.Load()),
                args=[ast.Constant(site), ast.Constant(OPS[type(node.ops[0])]), node.left, node.comparators[0]],
                keywords=[],
            )
            return ast.copy_location(call, node)

    class _Loader(importlib.abc.SourceLoader):
        def __init__(self, path):
            self.path = path

        def get_filename(self, fullname):
            return self.path

        def get_data(self, path):
            with open(path, "rb") as f:
                return f.read()

        def source_to_code(self, data, path, *, _optimize=-1):
            import hashlib
            import marshal
            cdir = os.path.join(_OUT, "_codecache")
            os.makedirs(cdir, exist_ok=True)
            h = hashlib.sha1(data + path.encode() + _SELF_HASH).hexdigest()
            cf = os.path.join(cdir, h)
            try:
                with open(cf, "rb") as f:
                    return marshal.loads(f.read())
            except OSError:
                pass
            code = self._compile(data, path)
            tmp = f"{cf}.{os.getpid()}"
            with open(tmp, "wb") as f:
                f.write(marshal.dumps(code))
            os.replace(tmp, cf)
            return code

        def _compile(self, data, path):
            tree = ast.parse(data, path)
            rel = os.path.realpath(path)[len(_ROOT):]
            t = _T(rel)
            tree = t.visit(tree)
            with open(os.path.join(_OUT, f"inst_{os.getpid()}.txt"), "a") as f:
                f.write("".join(x + "\n" for x in t.inst))
            ast.fix_missing_locations(tree)
            return compile(tree, path, "exec", dont_inherit=True)

        def path_stats(self, path):
            raise OSError  # never use cached bytecode

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if not (name == "sqlglot" or name.startswith("sqlglot.")):
                return None
            for finder in sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(name, path, target)
                if spec is not None:
                    break
            else:
                return None
            origin = spec.origin or ""
            if origin.endswith(".py") and os.path.realpath(origin).startswith(_ROOT + "sqlglot" + os.sep):
                spec.loader = _Loader(origin)
            return spec

    sys.meta_path.insert(0, _Finder())
