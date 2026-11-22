"""Per-query line map for the heavy TPC tests in tests/test_executor.py.

Each heavy test loops over a fixed list of queries (cls.tpch_sqls /
cls.tpcds_sqls). For every query i we run the test with that list cut down to
[i] in a forked child and record the lines it executes. The selector can then
re-run only the queries whose lines a patch touches.

usage: python unitmap.py SQLGLOT_DIR OUT.json.gz
"""
import gzip, json, os, sys, time

SRC = os.path.realpath(sys.argv[1]); OUT = sys.argv[2]
PREFIX = SRC + os.sep
os.environ["SKIP_INTEGRATION"] = "0"
sys.dont_write_bytecode = True
sys.path.insert(0, SRC)
os.chdir(SRC)

HEAVY = {
    "test_execute_tpcds": "tpcds_sqls",
    "test_execute_tpch": "tpch_sqls",
    "test_optimized_tpch": "tpch_sqls",
}

M = sys.monitoring
TOOL = M.COVERAGE_ID
HITS = set()


def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(PREFIX) and code.co_flags & 1:
        HITS.add((fn[len(PREFIX):], line))
    return M.DISABLE


def install_inline_pool():
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    import pickle

    class InlinePool:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
            if initializer is not None:
                fn, args = pickle.loads(pickle.dumps((initializer, initargs)))
                fn(*args)

        def submit(self, fn, *args, **kwargs):
            f = cf.Future()
            try:
                fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
                f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
            except BaseException as e:  # noqa
                f.set_exception(e)
            return f

        def shutdown(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cf.ProcessPoolExecutor = InlinePool
    cfp.ProcessPoolExecutor = InlinePool


def main():
    import unittest
    install_inline_pool()
    M.use_tool_id(TOOL, "units")
    M.register_callback(TOOL, M.events.LINE, on_line)
    M.set_events(TOOL, M.events.LINE)
    import tests.test_executor as te
    cls = te.TestExecutor
    # number of queries per list, from an unmonitored-equivalent setUpClass
    cls.setUpClass()
    counts = {attr: len(getattr(cls, attr)) for attr in set(HEAVY.values())}
    cls.tearDownClass()
    base = set(HITS)
    result = {}
    for test, attr in HEAVY.items():
        units = []
        for i in range(counts[attr]):
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                HITS.clear()
                M.restart_events()
                orig = cls.setUpClass.__func__

                @classmethod
                def setup(c, _orig=orig, _i=i, _attr=attr):
                    _orig(c)
                    setattr(c, _attr, [getattr(c, _attr)[_i]])

                cls.setUpClass = setup
                res = unittest.TestResult()
                unittest.TestSuite([cls(test)]).run(res)
                ok = res.wasSuccessful()
                d = {}
                for f, l in HITS:
                    d.setdefault(f, []).append(l)
                os.write(w, json.dumps({"ok": ok, "lines": d}).encode())
                os._exit(0)
            os.close(w)
            data = b""
            while True:
                ch = os.read(r, 1 << 20)
                if not ch:
                    break
                data += ch
            os.waitpid(pid, 0)
            os.close(r)
            u = json.loads(data)
            if not u["ok"]:
                print("unit failed at baseline", test, i, file=sys.stderr)
            units.append(u["lines"])
            print(test, i, sum(len(v) for v in u["lines"].values()), file=sys.stderr, flush=True)
        result[test] = units
    with gzip.open(OUT, "wt") as g:
        json.dump(result, g)


main()
