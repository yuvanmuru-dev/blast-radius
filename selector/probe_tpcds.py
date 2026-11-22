"""Stand-in for tests/test_executor.py::TestExecutor::test_execute_tpcds (21.5 s).

Run with cwd = a sqlglot tree. For every executed TPC-DS query it records what
the real test's result depends on: the DuckDB transpilation (reference side),
the optimized query and plan, every piece of Python code the executor
generates, and the rows produced on a small slice of each table. The selector
keeps the real test on any difference from the pristine baseline, and never
uses this stand-in when the executor package itself is patched.
"""
import ast
import csv
import gzip
import hashlib
import json
import sys

ROWS = 60


def main():
    sys.path.insert(0, ".")
    out = {}
    try:
        from sqlglot import exp, find_tables, parse_one, transpile  # noqa: F401
        from sqlglot.errors import ExecuteError  # noqa: F401
        from sqlglot.executor import env, execute  # noqa: F401
        from sqlglot.executor.python import Python, PythonExecutor  # noqa: F401
        from sqlglot.executor.table import Table, ensure_tables  # noqa: F401
        from sqlglot.optimizer import optimize
        from sqlglot.planner import Plan
        from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs
    except BaseException as e:  # noqa: BLE001
        print(json.dumps({"import": repr(e)}))
        return

    d = FIXTURES_DIR + "/optimizer/tpc-ds/"
    tables = {}
    for table, columns in TPCDS_SCHEMA.items():
        with gzip.open(f"{d}{table}.csv.gz", "rt", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="|")
            next(reader)
            rows, ctypes = [], []
            for k, row in enumerate(reader):
                if k >= ROWS:
                    break
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[table] = Table(columns=columns, rows=rows)

    gen = []
    g0 = PythonExecutor.generate

    def g(self, expression):
        r = g0(self, expression)
        gen.append(r.co_filename if r is not None else None)  # the generated source
        return r

    PythonExecutor.generate = g

    for i, (meta, sql, _) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
        rec = {}
        try:
            rec["duck"] = transpile(sql, write="duckdb")[0]
        except BaseException as e:  # noqa: BLE001
            rec["duck"] = repr(e)
        if meta.get("execute"):
            gen.clear()
            try:
                expression = parse_one(sql)
                names = sorted({t.name for t in find_tables(expression)})
                rec["tables"] = names
                o = optimize(expression.copy(), TPCDS_SCHEMA, leave_tables_isolated=True)
                rec["opt"] = o.sql()
                rec["plan"] = str(Plan(o))
                res = execute(expression, schema=TPCDS_SCHEMA, tables={n: tables[n] for n in names})
                rec["cols"] = list(res.columns)
                rec["rows"] = hashlib.sha1(repr(res.rows).encode()).hexdigest()
            except BaseException as e:  # noqa: BLE001
                rec["err"] = repr(e)
            rec["gen"] = hashlib.sha1("\n".join(map(str, gen)).encode()).hexdigest()
        out[i] = rec
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()
