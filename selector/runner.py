"""Runs selected sqlglot tests (unittest classes) in the overlay cwd.

stdin: JSON [[number, id, seconds], ...]. stdout: "BRPASS <number>" per test
that ran and passed. Everything else goes to /dev/null.
"""
import json
import os
import sys
import time
import unittest

def install_inline_pool():
    """Run process-pool work inline (pickled round trip), like the map builder."""
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
            except BaseException as e:  # noqa: BLE001
                f.set_exception(e)
            return f

        def map(self, fn, *its, timeout=None, chunksize=1):
            return [self.submit(fn, *a).result() for a in zip(*its)]

        def shutdown(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cf.ProcessPoolExecutor = InlinePool
    cfp.ProcessPoolExecutor = InlinePool


def main():
    install_inline_pool()
    req = json.loads(sys.stdin.read())
    import fcntl
    global report
    report = fcntl.fcntl(1, fcntl.F_DUPFD, 200)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    sys.path.insert(0, os.getcwd())


    def emit(nums):
        if nums:
            os.write(report, "".join(f"BRPASS {n}\n" for n in nums).encode())


    groups = {}  # (module, class) -> [(num, method, secs)]
    order = []
    for num, tid, secs in sorted(req):
        parts = tid.split("::")
        if len(parts) != 3 or not parts[0].endswith(".py"):
            continue  # unknown shape: never confirmed, so kept
        mod = parts[0][:-3].replace("/", ".")
        key = (mod, parts[1])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((num, parts[2], secs))

    mods = {}
    for mod, _ in order:
        if mod in mods:
            continue
        try:
            __import__(mod)
            mods[mod] = sys.modules[mod]
        except BaseException:  # noqa: BLE001 - import failure means the tests fail
            mods[mod] = None

    for key in order:
        mod, cls_name = key
        m = mods.get(mod)
        if m is None:
            continue
        cls = getattr(m, cls_name, None)
        if cls is None:
            continue
        items = groups[key]
        budget = sum(s for _, _, s in items) * 4 + 5
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            ok = []
            try:
                suite = unittest.TestSuite()
                tests = []
                for num, meth, _ in items:
                    t = cls(meth)
                    tests.append((num, t))
                    suite.addTest(t)
                res = unittest.TestResult()
                suite.run(res)
                bad = set()
                for t, _ in res.failures + res.errors:
                    bad.add(id(getattr(t, "test_case", t)))
                for t in res.unexpectedSuccesses:
                    bad.add(id(t))
                # class-level fixture errors appear as _ErrorHolder: fail everything
                if any(not isinstance(getattr(t, "test_case", t), unittest.TestCase) for t, _ in res.errors):
                    bad = {id(t) for _, t in tests}
                ok = [num for num, t in tests if id(t) not in bad]
            except BaseException:  # noqa: BLE001
                ok = []
            os.write(w, json.dumps(ok).encode())
            os._exit(0)
        os.close(w)
        t0 = time.time()
        data = b""
        import select
        killed = False
        while True:
            left = budget - (time.time() - t0)
            if left <= 0:
                os.kill(pid, 9)
                killed = True
                break
            rr, _, _ = select.select([r], [], [], min(left, 1.0))
            if rr:
                chunk = os.read(r, 65536)
                if not chunk:
                    break
                data += chunk
        os.waitpid(pid, 0)
        os.close(r)
        if not killed:
            try:
                emit(json.loads(data.decode() or "[]"))
            except ValueError:
                pass


if __name__ == "__main__":
    main()
