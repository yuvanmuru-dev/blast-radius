"""Per-test map of which elements of class-level / module-level SETS each test
asks about (x in S). Derived from keymap.py (dicts); only membership tests are
recorded: the selector uses these reads only for set attributes whose every
runtime use in the source is an `in` / `not in` test (static gate), so other
reads (iteration, set algebra) never need to be seen here.

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


class _SetMixin:
    def _hit(self, k):
        if not IMPORTING[0]:
            d = kd(k)
            for o, a in self._br_names:
                ACC.add((o, a, d))

    def __contains__(self, k):
        self._hit(k)
        return super().__contains__(k)


class TSet(_SetMixin, set):
    __slots__ = ("_br_names",)
    __hash__ = None


class TFrozenSet(_SetMixin, frozenset):
    __slots__ = ("_br_names",)

    def __hash__(self):
        return frozenset.__hash__(self)


DONE = set()
WRAPPED = {}  # id(original dict) -> TDict


def _wrap(v, owner, attr):
    """Return the TSet/TFrozenSet standing in for set v (shared by all its names)."""
    if isinstance(v, (TSet, TFrozenSet)):
        if (owner, attr) not in v._br_names:
            v._br_names.append((owner, attr))
        return v
    t = WRAPPED.get(id(v))
    if t is None:
        t = TSet(v) if type(v) is set else TFrozenSet(v)
        t._br_names = []
        WRAPPED[id(v)] = t
        KEEP.append(v)
    if (owner, attr) not in t._br_names:
        t._br_names.append((owner, attr))
    return t


KEEP = []


def instrument_module(m):
    # module-level dict globals, then classes (aliases share one TDict)
    for name, v in list(vars(m).items()):
        if (type(v) in (set, frozenset) or isinstance(v, (TSet, TFrozenSet))) and not name.startswith("__"):
            try:
                setattr(m, name, _wrap(v, "module:" + m.__name__, name))
            except Exception:  # noqa: BLE001
                pass
    for name, v in list(vars(m).items()):
        if isinstance(v, type) and getattr(v, "__module__", None) == m.__name__:
            instrument_class(v)


def instrument_class(cls):
    if id(cls) in DONE:
        return
    DONE.add(id(cls))
    owner = f"{cls.__module__}.{cls.__qualname__}"
    for attr, v in list(cls.__dict__.items()):
        if (type(v) in (set, frozenset) or isinstance(v, (TSet, TFrozenSet))) and not attr.startswith("__"):
            try:
                setattr(cls, attr, _wrap(v, owner, attr))
            except (AttributeError, TypeError):
                pass
        elif isinstance(v, type) and str(getattr(v, "__module__", "")).startswith("sqlglot"):
            instrument_class(v)


def instrument_all():
    mods = [m for n, m in list(sys.modules.items()) if m is not None and (n == "sqlglot" or n.startswith("sqlglot."))]
    # two passes so every alias of a dict joins the same TDict before classes are walked
    for m in mods:
        for name, v in list(vars(m).items()):
            if type(v) in (set, frozenset) and not name.startswith("__"):
                _wrap(v, "module:" + m.__name__, name)
    for m in mods:
        for name, v in list(vars(m).items()):
            if isinstance(v, type) and getattr(v, "__module__", "").startswith("sqlglot"):
                for attr, x in list(v.__dict__.items()):
                    if type(x) in (set, frozenset) and not attr.startswith("__"):
                        _wrap(x, f"{v.__module__}.{v.__qualname__}", attr)
    for m in mods:
        instrument_module(m)
    # aliases held by test modules / test classes (from x import D)
    for n, m in list(sys.modules.items()):
        if m is None or not (n == "tests" or n.startswith("tests.")):
            continue
        for name, v in list(vars(m).items()):
            if type(v) in (set, frozenset) and id(v) in WRAPPED:
                setattr(m, name, WRAPPED[id(v)])
            elif isinstance(v, type) and getattr(v, "__module__", "") == n:
                for attr, x in list(v.__dict__.items()):
                    if type(x) in (set, frozenset) and id(x) in WRAPPED:
                        try:
                            setattr(v, attr, WRAPPED[id(x)])
                        except (AttributeError, TypeError):
                            pass


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
        t._br_names = list(getattr(src, "_br_names", [(f"{cls.__module__}.{cls.__qualname__}", "TRANSFORMS")]))
        return t

    _build_dispatch._br = True
    g._build_dispatch = _build_dispatch


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
