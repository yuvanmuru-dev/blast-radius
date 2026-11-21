"""Per-test dependency map of sqlglot at the pinned commit.

For each test file: fork a "zygote" from a bare interpreter, trace while it
imports the test module (import-time lines + files opened), then fork one
child per test that runs just that test (setUpClass included) and records the
lines it newly executes and the files it opens. A test's deps are
zygote deps U child deps, i.e. exactly what "import the file, run this test"
touches. Process pools are replaced by an inline executor so their work is
traced too.

usage: python mapper.py SQLGLOT_DIR TESTS_JSON OUTDIR [-j 2] [--files f1,f2]
Writes OUTDIR/<test number>.json = {"lines": {relfile: [..]}, "opens": [..],
"ok": bool, "sub": bool}
"""
import argparse, json, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("src"); ap.add_argument("tests_json"); ap.add_argument("outdir")
ap.add_argument("-j", type=int, default=2)
ap.add_argument("--files", default="")
ap.add_argument("--timeout", type=int, default=600)
A = ap.parse_args()

SRC = os.path.realpath(A.src)
PREFIX = SRC + os.sep
os.environ["SKIP_INTEGRATION"] = "0"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, SRC)
os.chdir(SRC)
os.makedirs(A.outdir, exist_ok=True)

tests = json.load(open(A.tests_json))["tests"]
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t)
files = [f for f in by_file if not A.files or f in A.files.split(",")]

M = sys.monitoring
TOOL = M.COVERAGE_ID
HITS = set()
OPENS = set()
SUBPROC = [False]


def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(PREFIX):
        HITS.add((fn[len(PREFIX):], line, code.co_flags & 1))
    return M.DISABLE


RECV = set()
_MRO_CACHE = {}


def _mro_names(cls):
    r = _MRO_CACHE.get(cls)
    if r is None:
        r = tuple(
            f"{c.__module__}.{c.__qualname__}"
            for c in getattr(cls, "__mro__", ())
            if isinstance(getattr(c, "__module__", None), str)
            and (c.__module__.startswith("sqlglot") or c.__module__.startswith("tests"))
        )
        _MRO_CACHE[cls] = r
    return r


IMPORTING = [0]


def install_import_guard():
    import importlib._bootstrap as bs
    orig = bs._load_unlocked

    def _load_unlocked(spec):
        IMPORTING[0] += 1
        try:
            return orig(spec)
        finally:
            IMPORTING[0] -= 1

    bs._load_unlocked = _load_unlocked


def on_start(code, off):
    if IMPORTING[0]:
        return
    if code.co_argcount and code.co_filename.startswith(PREFIX):
        v0 = code.co_varnames[0]
        if v0 == "self" or v0 == "cls":
            try:
                obj = sys._getframe(1).f_locals[v0]
            except Exception:
                return
            cls = obj if isinstance(obj, type) else type(obj)
            r = _MRO_CACHE.get(cls)
            if r is None:
                r = _mro_names(cls)
            RECV.update(r)


def audit(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                p = os.path.realpath(os.fsdecode(p))
            except Exception:
                return
            if p.startswith(PREFIX):
                OPENS.add(p[len(PREFIX):])
    elif event in ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.fork"):
        SUBPROC[0] = True


def install_inline_pool():
    import concurrent.futures as cf
    import pickle

    class InlinePool:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
            if initializer is not None:
                fn, args = pickle.loads(pickle.dumps((initializer, initargs)))
                fn(*args)

        def submit(self, fn, *args, **kwargs):
            f = cf.Future()
            try:
                fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
                f.set_result(pickle.loads(pickle.dumps(fn2(*args2, **kwargs2))))
            except BaseException as e:  # noqa
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
    import concurrent.futures.process as cfp
    cfp.ProcessPoolExecutor = InlinePool


def dump(num, payload):
    tmp = os.path.join(A.outdir, f".{num}.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, os.path.join(A.outdir, f"{num}.json"))


def lines_dict(hits, kind):
    d = {}
    for f, l, k in hits:
        if k == kind:
            d.setdefault(f, []).append(l)
    for v in d.values():
        v.sort()
    return d


def run_file(tfile):
    """Runs inside the zygote process for one test file."""
    import unittest
    install_inline_pool()
    install_import_guard()
    sys.addaudithook(audit)
    M.use_tool_id(TOOL, "br")
    M.register_callback(TOOL, M.events.LINE, on_line)
    M.register_callback(TOOL, M.events.PY_START, on_start)
    M.set_events(TOOL, M.events.LINE | M.events.PY_START)
    modname = tfile[:-3].replace("/", ".")
    import_ok = True
    try:
        __import__(modname)
    except BaseException as e:  # noqa
        import_ok = False
        print("IMPORT FAIL", tfile, e, file=sys.stderr)
    zy_hits, zy_opens, zy_recv = set(HITS), set(OPENS), set(RECV)
    loader = unittest.TestLoader()
    for t in by_file[tfile]:
        num = t["number"]
        if os.path.exists(os.path.join(A.outdir, f"{num}.json")):
            continue
        HITS.clear(); OPENS.clear(); RECV.clear(); SUBPROC[0] = False
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            SUBPROC[0] = False
            ok, sub = import_ok, False
            try:
                parts = t["id"].split("::")
                name = modname + "." + ".".join(parts[1:])
                suite = loader.loadTestsFromName(name)
                res = unittest.TestResult()
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                suite.run(res)
                ok = ok and res.wasSuccessful()
            except BaseException as e:  # noqa
                ok = False
                print("RUN FAIL", t["id"], e, file=sys.stderr)
            payload = {
                "id": t["id"],
                "lines": lines_dict(zy_hits | HITS, 0),
                "flines": lines_dict(zy_hits | HITS, 1),
                "opens": sorted(zy_opens | OPENS),
                "recv": sorted(zy_recv | RECV),
                "ok": ok,
                "subproc": SUBPROC[0],
            }
            dump(num, payload)
            os._exit(0)
        os.close(w)
        t0 = time.time()
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if time.time() - t0 > A.timeout:
                os.kill(pid, 9); os.waitpid(pid, 0)
                print("TIMEOUT", t["id"], file=sys.stderr)
                break
            time.sleep(0.01)
        os.close(r)


def main():
    pending = list(files)
    # biggest files first for better parallel balance
    pending.sort(key=lambda f: -sum(t["seconds"] for t in by_file[f]))
    running = {}
    t0 = time.time()
    while pending or running:
        while pending and len(running) < A.j:
            f = pending.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    run_file(f)
                finally:
                    os._exit(0)
            running[pid] = (f, time.time())
        pid, _ = os.wait()
        f, ts = running.pop(pid)
        print(f"done {f} {time.time()-ts:.1f}s (total {time.time()-t0:.0f}s)", file=sys.stderr, flush=True)


main()
