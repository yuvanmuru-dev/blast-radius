"""Scaled stand-in for tests/test_parser.py::TestParser::test_max_nodes (28.8 s).

Run with cwd = a sqlglot tree (pristine or patched overlay). Prints one JSON
document describing everything the real test depends on. The selector compares
it with the pristine baseline and keeps the real test on any difference.

Kept exact: the test module import (and with it every import the real test
needs), the small parses, the max_nodes=5 parse and the parse after it.
Scaled: the 10000-wide, 100-deep query becomes WIDTH wide with the same depth;
it is parsed under a ladder of limits, recording where and how each one stops,
so any change to node counting, error raising or message text shows up.
"""
import json
import sys

WIDTH = 40
DEPTH = 100
LIMITS = (0, 1, 5, 50, 777, 3000, 4000, 5000, 6000, 7000, 7999, 50_000, 1_000_000)


def outcome(fn):
    try:
        r = fn()
        return ["ok", type(r).__module__, type(r).__qualname__]
    except BaseException as e:  # noqa: BLE001
        info = ["err", type(e).__module__, type(e).__qualname__, str(e)]
        errs = getattr(e, "errors", None)
        if isinstance(errs, list):
            info.append(json.dumps(errs, sort_keys=True, default=repr))
        return info


def main():
    sys.path.insert(0, ".")
    out = {}
    try:
        import tests.test_parser as tp  # noqa: F401  same imports as the real test
        from sqlglot import parse_one, exp
        from sqlglot.errors import ParseError
        out["names"] = [exp.Select.__qualname__, ParseError.__qualname__]
    except BaseException as e:  # noqa: BLE001
        out["import"] = repr(e)
        print(json.dumps(out))
        return

    out["one"] = outcome(lambda: parse_one("SELECT 1", max_nodes=1_000_000))
    cols = [f"col{i}" for i in range(500)]
    sql500 = f"SELECT {','.join(cols)} FROM t"
    out["c500"] = outcome(lambda: parse_one(sql500, max_nodes=1_000_000))
    r = None
    try:
        r = parse_one(sql500, max_nodes=1_000_000)
        out["c500n"] = sum(1 for _ in r.walk())
    except BaseException as e:  # noqa: BLE001
        out["c500n"] = repr(e)

    expr = "x" + "=x" * DEPTH
    sql = f"SELECT {','.join(expr for _ in range(WIDTH))}"
    for lim in LIMITS:
        out[f"w{lim}"] = outcome(lambda: parse_one(sql, max_nodes=lim))
    try:
        t = parse_one(sql)
        out["wn"] = sum(1 for _ in t.walk())
    except BaseException as e:  # noqa: BLE001
        out["wn"] = repr(e)

    # execution-path fingerprint: count every line run while parsing a small
    # version of the wide query; extra loop iterations (e.g. a scanner that no
    # longer stops early, quadratic at full size) change the count
    out["lines"] = _count_lines(lambda: parse_one(f"SELECT {','.join(expr for _ in range(4))}", max_nodes=1_000_000))
    out["lines_err"] = _count_lines(lambda: parse_one(f"SELECT {','.join(expr for _ in range(4))}", max_nodes=500))

    small = "SELECT " + ",".join(f"x={i}" for i in range(100))
    out["s5"] = outcome(lambda: parse_one(small, max_nodes=5))
    out["after"] = outcome(lambda: parse_one("SELECT 1, 2, 3"))
    print(json.dumps(out, sort_keys=True))


def _count_lines(fn):
    mon = sys.monitoring
    tid = 4
    counts = {}

    def on_line(code, line):
        k = (code.co_filename.rsplit("sqlglot", 1)[-1], code.co_qualname, line)
        counts[k] = counts.get(k, 0) + 1

    try:
        mon.use_tool_id(tid, "br-probe")
    except ValueError:
        return "busy"
    mon.register_callback(tid, mon.events.LINE, on_line)
    mon.set_events(tid, mon.events.LINE)
    try:
        r = outcome(fn)
    finally:
        mon.set_events(tid, 0)
        mon.register_callback(tid, mon.events.LINE, None)
        mon.free_tool_id(tid)
    total = sum(counts.values())
    return [r, total, len(counts)]


if __name__ == "__main__":
    main()
