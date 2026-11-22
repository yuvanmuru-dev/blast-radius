"""Stand-in for tests/test_executor.py::TestExecutor::test_execute_tpcds when a
commit only changes optimizer code (never scope.py or eliminate_joins.py, which
the planner and executor also use at run time).

Run with cwd = a sqlglot tree. For every TPC-DS query the test executes it
prints a digest of what the executor's result depends on that the optimizer
can change: the optimized tree in full (node classes, every arg, inferred
types and meta) and the DuckDB transpilation the reference side runs. The
parse of every query (also done by the test) is included. Identical digests
mean the planner and executor see identical input, so the test behaves
identically.
"""
import hashlib
import json
import sys


def dump(node, seen):
    from sqlglot import exp
    if isinstance(node, exp.Expr):
        if id(node) in seen:
            return ("cycle",)
        seen.add(id(node))
        args = tuple((k, dump(v, seen)) for k, v in sorted(node.args.items()))
        t = getattr(node, "_type", None)
        meta = getattr(node, "_meta", None)
        out = (type(node).__module__, type(node).__qualname__, args,
               dump(t, set()) if t is not None and t is not node else None,
               repr(sorted(meta.items(), key=lambda kv: kv[0])) if meta else None,
               repr(node.comments) if getattr(node, "comments", None) else None)
        seen.discard(id(node))
        return out
    if isinstance(node, (list, tuple)):
        return (type(node).__name__,) + tuple(dump(v, seen) for v in node)
    return ("v", type(node).__name__, repr(node))


def main():
    sys.path.insert(0, ".")
    sys.setrecursionlimit(20000)
    out = {}
    try:
        from sqlglot import parse_one, transpile
        from sqlglot.optimizer import optimize
        from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs
    except BaseException as e:  # noqa: BLE001
        print(json.dumps({"import": repr(e)}))
        return
    import os
    base = None
    cmp_path = os.environ.get("BR_TPCDS_CMP")
    if cmp_path:
        try:
            with open(cmp_path) as f:
                base = json.loads(json.load(f)["probe_tpcds_opt.py"])
        except BaseException:  # noqa: BLE001
            print(json.dumps({"same": False, "at": "nobase"}))
            return
    for i, (meta, sql, _) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
        rec = []
        try:
            e = parse_one(sql)
            rec.append(repr(dump(e, set())))
            if meta.get("execute"):
                rec.append(repr(transpile(sql, write="duckdb")))
                o = optimize(e.copy(), TPCDS_SCHEMA, leave_tables_isolated=True)
                rec.append(repr(dump(o, set())))
        except BaseException as ex:  # noqa: BLE001
            rec.append("ERR " + repr(ex))
        out[i] = hashlib.sha1("\n".join(rec).encode()).hexdigest()
        if base is not None and base.get(str(i)) != out[i]:
            print(json.dumps({"same": False, "at": i}))  # stop at the first difference
            return
    if base is not None:
        print(json.dumps({"same": len(base) == len(out)}))
        return
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()
