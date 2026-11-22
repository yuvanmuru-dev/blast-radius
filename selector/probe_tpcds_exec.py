"""Stand-in for tests/test_executor.py::TestExecutor::test_execute_tpcds when a
commit only changes the Python executor (sqlglot/executor/*.py).

Run with cwd = a sqlglot tree. Does what the test's worker does for every
executed TPC-DS query: builds the tables the way setUpClass does (same Table
class, same typed rows), ships the parsed query and the tables through pickle
like the process pool, runs sqlglot.executor.execute with the test's schema,
and ships the result back through pickle. Prints a digest per query (columns
and rows, or the error). Parse, optimizer, planner and DuckDB side are not
touched by such a commit, so identical results mean the test sees exactly
what it saw before.

With BR_TPCDS_CMP set it compares against the baseline as it goes and stops at
the first difference. BR_TPCDS_ONLY restricts to some query indices.
"""
import ast
import csv
import gzip
import hashlib
import json
import os
import pickle
import sys


def main():
    sys.path.insert(0, ".")
    try:
        from sqlglot import find_tables, parse_one
        from sqlglot.executor import execute
        from sqlglot.executor.table import Table
        from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs
    except BaseException as e:  # noqa: BLE001
        print(json.dumps({"same": False, "at": "import " + repr(e)[:80]}))
        return
    base = None
    cmp_path = os.environ.get("BR_TPCDS_CMP")
    if cmp_path:
        try:
            with open(cmp_path) as f:
                base = json.loads(json.load(f)["probe_tpcds_exec.py"])
        except BaseException:  # noqa: BLE001
            print(json.dumps({"same": False, "at": "nobase"}))
            return
    only = os.environ.get("BR_TPCDS_ONLY")
    only = None if only is None else {int(x) for x in only.split(",") if x}

    pairs = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    todo = [(i, sql) for i, (meta, sql, _) in enumerate(pairs)
            if meta.get("execute") and (only is None or i in only)]
    d = FIXTURES_DIR + "/optimizer/tpc-ds/"
    cache = {}

    def table(name):
        # exactly setUpClass: literal_eval typing from the first row, Table(columns=dict, rows)
        if name not in cache:
            columns = TPCDS_SCHEMA[name]
            with gzip.open(f"{d}{name}.csv.gz", "rt", encoding="utf-8") as f:
                reader = csv.reader(f, delimiter="|")
                next(reader)
                rows, ctypes = [], []
                for row in reader:
                    if not ctypes:
                        for v in row:
                            try:
                                ctypes.append(type(ast.literal_eval(v)))
                            except (ValueError, SyntaxError):
                                ctypes.append(str)
                    rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
            cache[name] = Table(columns=columns, rows=rows)
        return cache[name]

    out = {}
    shipped = [None]

    def all_tables():
        # the pool ships every table once per worker through its initializer
        if shipped[0] is None:
            shipped[0] = pickle.loads(pickle.dumps((TPCDS_SCHEMA, {n: table(n) for n in TPCDS_SCHEMA})))
        return shipped[0]

    dump_path = os.environ.get("BR_TPCDS_DUMP_OPT")
    if dump_path:
        # build time: the pristine optimized tree of every executed query, made
        # exactly as execute() makes it (from the pickled parse, with ensure_schema)
        from sqlglot.optimizer import optimize
        from sqlglot.schema import ensure_schema
        trees = {}
        for i, sql in todo:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
            trees[i] = optimize(e, ensure_schema(TPCDS_SCHEMA), leave_tables_isolated=True)
        with open(dump_path, "wb") as f:
            pickle.dump(trees, f)
        print(json.dumps({"dumped": len(trees)}))
        return
    fast = None
    fast_path = os.environ.get("BR_TPCDS_FAST")
    if fast_path:
        # executor/__init__.py unchanged: mirror execute() around the cached
        # optimized trees (optimizer and planner are untouched by this commit)
        from sqlglot.errors import ExecuteError
        from sqlglot.executor.python import PythonExecutor
        from sqlglot.executor.table import ensure_tables
        from sqlglot.planner import Plan
        from sqlglot.schema import ensure_schema
        with open(fast_path, "rb") as f:
            fast = pickle.load(f)

        def run_fast(i, tables, schema):
            tables_ = ensure_tables(tables, dialect=None)
            schema_ = ensure_schema(schema, dialect=None)
            if tables_.supported_table_args and tables_.supported_table_args != schema_.supported_table_args:
                raise ExecuteError("Tables must support the same table args as schema")
            return PythonExecutor(tables=tables_).execute(Plan(fast[i].copy()))

    for i, sql in todo:
        try:
            expression = parse_one(sql)
            names = [t.name for t in find_tables(expression)]
            schema, everything = all_tables()
            tables = {n: everything[n] for n in names}
            expression = pickle.loads(pickle.dumps(expression))  # pool.submit
            if fast is not None and i in fast:
                res = run_fast(i, tables, schema)
            else:
                res = execute(expression, schema=schema, tables=tables)
            res = pickle.loads(pickle.dumps(res))  # future.result()
            rec = repr((list(res.columns), [tuple(r) for r in res.rows]))
        except BaseException as ex:  # noqa: BLE001
            rec = "ERR " + repr(ex)
        h = hashlib.sha1(rec.encode()).hexdigest()
        out[f"x{i}"] = h
        if base is not None and base.get(f"x{i}") != h:
            print(json.dumps({"same": False, "at": f"x{i}"}))
            return
    if base is not None:
        print(json.dumps({"same": True}))
        return
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()
