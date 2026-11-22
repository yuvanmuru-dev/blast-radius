"""Per-test map of which entries of class-level dicts each test reads.

Every dict stored as a class attribute of a sqlglot class is swapped for a
recording subclass (same contents) before the test runs, and classes created
by lazy imports during the test are instrumented as they appear. A lookup
records (owner class, attribute, digest of key); anything that reads the
whole dict (iteration, copy, len, ==, |) records a wildcard for it. Reads that
happen while a module is being imported are ignored: import-time effects are
covered by the selector's fingerprint diff.

usage: python keymap.py SQLGLOT_DIR TESTS_JSON OUTDIR [-j 2] [--files a,b]
Writes OUTDIR/<num>.json = {"k": [[owner, attr, kd], ...], "w": [[owner, attr], ...]}
"""
import argparse, json, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("src"); ap.add_argument("tests_json"); ap.add_argument("outdir")
ap.add_argument("-j", type=int, default=2)
ap.add_argument("--files", default="")
ap.add_argument("--timeout", type=int, default=900)
A = ap.parse_args()

SRC = os.path.realpath(A.src)
os.environ["SKIP_INTEGRATION"] = "0"
sys.dont_write_bytecode = True
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
os.chdir(SRC)
os.makedirs(A.outdir, exist_ok=True)
import fp as FP  # key digests must match the selector's fingerprint

tests = json.load(open(A.tests_json))["tests"]
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t)
files = [f for f in by_file if not A.files or f in A.files.split(",")]

IMPORTING = [0]
ACC = set()
WILD = set()
KD_CACHE = {}


def kd(k):
    try:
        r = KD_CACHE.get(k)
    except TypeError:
        r = None
    if r is None:
        r = FP.digest(FP.h(k))
        try:
            KD_CACHE[k] = r
        except TypeError:
            pass
    return r


class TDict(dict):
    __slots__ = ("_br_owner", "_br_attr")

    def _hit(self, k):
        if not IMPORTING[0]:
            ACC.add((self._br_owner, self._br_attr, kd(k)))

    def _wild(self):
        if not IMPORTING[0]:
            WILD.add((self._br_owner, self._br_attr))

    def __getitem__(self, k):
        self._hit(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._hit(k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        self._hit(k)
        return dict.__contains__(self, k)

    def setdefault(self, k, default=None):
        self._hit(k)
        return dict.setdefault(self, k, default)

    def pop(self, k, *a):
        self._hit(k)
        return dict.pop(self, k, *a)

    def __iter__(self):
        self._wild()
        return dict.__iter__(self)

    def keys(self):
        self._wild()
        return dict.keys(self)

    def items(self):
        self._wild()
        return dict.items(self)

    def values(self):
        self._wild()
        return dict.values(self)

    def __len__(self):
        self._wild()
        return dict.__len__(self)

    def copy(self):
        self._wild()
        return dict(dict.items(self))

    def __eq__(self, o):
        self._wild()
        return dict.__eq__(self, o)

    def __ne__(self, o):
        self._wild()
        return dict.__ne__(self, o)

    __hash__ = None

    def __or__(self, o):
        self._wild()
        return dict(dict.items(self)) | o

    def __ror__(self, o):
        self._wild()
        return o | dict(dict.items(self))

    def __reversed__(self):
        self._wild()
        return dict.__reversed__(self)

    def __repr__(self):
        self._wild()
        return dict.__repr__(self)

    def __reduce_ex__(self, p):
        self._wild()
        return (dict, (dict(dict.items(self)),))

    def __bool__(self):
        self._wild()
        return dict.__len__(self) > 0


DONE = set()


def instrument_module(m):
    for name, v in list(vars(m).items()):
        if isinstance(v, type) and getattr(v, "__module__", None) == m.__name__:
            instrument_class(v)


def instrument_class(cls):
    if id(cls) in DONE:
        return
    DONE.add(id(cls))
    owner = f"{cls.__module__}.{cls.__qualname__}"
    for attr, v in list(cls.__dict__.items()):
        if type(v) is dict and not attr.startswith("__"):
            t = TDict(v)
            t._br_owner = owner
            t._br_attr = attr
            try:
                setattr(cls, attr, t)
            except (AttributeError, TypeError):
                pass
        elif isinstance(v, type) and str(getattr(v, "__module__", "")).startswith("sqlglot"):
            instrument_class(v)


def hook_dispatch():
    """sqlglot.generator._build_dispatch copies cls.TRANSFORMS into a per-class
    dispatch dict cached at run time. Keep key-level tracking through it: the
    copy is not a wildcard read, and lookups in the cached dict count as reads
    of TRANSFORMS on the class that defines it."""
    import sqlglot.generator as g
    orig = g._build_dispatch
    if getattr(orig, "_br", False):
        return

    def _build_dispatch(cls):
        IMPORTING[0] += 1
        try:
            d = orig(cls)
        finally:
            IMPORTING[0] -= 1
        src = cls.TRANSFORMS
        t = TDict(d)
        t._br_owner = getattr(src, "_br_owner", f"{cls.__module__}.{cls.__qualname__}")
        t._br_attr = "TRANSFORMS"
        return t

    _build_dispatch._br = True
    g._build_dispatch = _build_dispatch


def instrument_all():
    for name, m in list(sys.modules.items()):
        if name == "sqlglot" or name.startswith("sqlglot."):
            if m is not None:
                instrument_module(m)


def install_hooks():
    import importlib._bootstrap as bs
    orig = bs._load_unlocked

    def _load_unlocked(spec):
        IMPORTING[0] += 1
        try:
            mod = orig(spec)
        finally:
            IMPORTING[0] -= 1
        if spec.name == "sqlglot" or spec.name.startswith("sqlglot."):
            try:
                instrument_module(mod)
            except Exception:  # noqa: BLE001
                pass
        return mod

    bs._load_unlocked = _load_unlocked


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


def run_file(tfile):
    import unittest
    install_inline_pool()
    install_hooks()
    modname = tfile[:-3].replace("/", ".")
    ok_import = True
    try:
        __import__(modname)
    except BaseException as e:  # noqa: BLE001
        ok_import = False
    instrument_all()
    try:
        hook_dispatch()
    except Exception:  # noqa: BLE001
        pass
    loader = unittest.TestLoader()
    for t in by_file[tfile]:
        num = t["number"]
        outp = os.path.join(A.outdir, f"{num}.json")
        if os.path.exists(outp):
            continue
        pid = os.fork()
        if pid == 0:
            ACC.clear(); WILD.clear()
            ok = ok_import
            try:
                parts = t["id"].split("::")
                suite = loader.loadTestsFromName(modname + "." + ".".join(parts[1:]))
                res = unittest.TestResult()
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                suite.run(res)
                ok = ok and res.wasSuccessful()
            except BaseException:  # noqa: BLE001
                ok = False
            tmp = outp + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"id": t["id"], "ok": ok, "k": sorted(ACC), "w": sorted(WILD)}, f)
            os.replace(tmp, outp)
            os._exit(0)
        t0 = time.time()
        while True:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if time.time() - t0 > A.timeout:
                os.kill(pid, 9); os.waitpid(pid, 0)
                print("TIMEOUT", t["id"], file=sys.stderr)
                break
            time.sleep(0.01)


def main():
    pending = sorted(files, key=lambda f: -sum(t["seconds"] for t in by_file[f]))
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
