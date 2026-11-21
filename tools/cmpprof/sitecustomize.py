"""Comparison-site profiler, auto-loaded (sitecustomize) by every Python process
started with this directory on PYTHONPATH, including spawned pool workers.

Every single-operator comparison in sqlglot/ (is, is not, ==, !=, <, <=, >, >=)
is rewritten to call __br_cmp__, which returns exactly the original result and
records, per site, whether `is` and `==` ever disagreed and whether the operands
were ever equal. A swap like `is` <-> `==` at a site where they never disagreed
cannot change any test's execution.
"""
import ast
import builtins
import importlib.abc
import importlib.util
import os
import sys

_ROOT = os.environ.get("BRPROF_ROOT")
_OUT = os.environ.get("BRPROF_OUT", "/tmp/brprof")
if _ROOT:
    _ROOT = os.path.realpath(_ROOT) + os.sep
    os.makedirs(_OUT, exist_ok=True)
    _fh = [None]
    _flags = {}

    def _note(site, flag):
        key = (site, flag)
        if key in _flags:
            return
        _flags[key] = 1
        if _fh[0] is None:
            _fh[0] = open(os.path.join(_OUT, f"{os.getpid()}.txt"), "a", buffering=1)
        _fh[0].write(f"{site}\t{flag}\n")

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
        _note(site, "seen")
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
        else:
            if eqb:
                _note(site, "equal")
        return res

    builtins.__br_cmp__ = __br_cmp__
    OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3, ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}

    class _T(ast.NodeTransformer):
        def __init__(self, rel):
            self.rel = rel

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
            tree = ast.parse(data, path)
            rel = os.path.realpath(path)[len(_ROOT):]
            tree = _T(rel).visit(tree)
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
