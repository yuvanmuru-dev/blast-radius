"""Import-time fingerprint of sqlglot: a stable hash of every module global and
every class attribute, so a class-level / module-level edit can be traced to
exactly the classes whose import-time state it changes (dict spreads, metaclass
derived tables, closures built at import, ...).

Function *code* is deliberately left out of the hash: edits inside function
bodies are handled by the line map. Signatures, defaults and closure contents
are hashed.

usage (child process, cwd = tree root on sys.path):
    python fp.py OUT.json [module ...]     # hash only these modules (default all)
"""
import enum
import functools
import hashlib
import importlib
import json
import os
import re
import sys
import types

MAXDEPTH = 250


def _code_sig(c):
    return f"{c.co_argcount},{c.co_posonlyargcount},{c.co_kwonlyargcount},{c.co_varnames[:c.co_argcount + c.co_kwonlyargcount]},{c.co_flags & 0x0C}"


def h(v, depth=0, seen=None):
    if seen is None:
        seen = set()
    t = type(v)
    if v is None or t in (bool, int, float, complex, str, bytes):
        return t.__name__ + ":" + repr(v)
    if isinstance(v, enum.Enum):
        return f"E:{type(v).__module__}.{type(v).__qualname__}.{v.name}"
    if isinstance(v, type):
        return f"T:{v.__module__}.{v.__qualname__}"
    if isinstance(v, types.ModuleType):
        return "M:" + v.__name__
    if depth > MAXDEPTH:
        return "D:" + t.__name__
    if id(v) in seen:
        return "C:" + t.__name__
    seen = seen | {id(v)}
    if isinstance(v, (types.FunctionType,)):
        if SHALLOW[0]:
            if "<" in v.__qualname__:
                SHALLOW[1] = True  # anonymous function inside: cannot defer to its own key
            return "F:" + v.__qualname__ + "|" + _code_sig(v.__code__)
        parts = [v.__qualname__, _code_sig(v.__code__)]
        if v.__defaults__:
            parts.append(h(v.__defaults__, depth + 1, seen))
        if v.__kwdefaults__:
            parts.append(h(v.__kwdefaults__, depth + 1, seen))
        if v.__closure__:
            for cell in v.__closure__:
                try:
                    parts.append(h(cell.cell_contents, depth + 1, seen))
                except ValueError:
                    parts.append("empty")
        return "F:" + "|".join(parts)
    if isinstance(v, (staticmethod, classmethod)):
        return t.__name__ + ":" + h(v.__func__, depth + 1, seen)
    if isinstance(v, property):
        return "P:" + "|".join(h(f, depth + 1, seen) for f in (v.fget, v.fset, v.fdel))
    if isinstance(v, types.MethodType):
        return "BM:" + h(v.__func__, depth + 1, seen) + "@" + h(v.__self__, depth + 1, seen)
    if isinstance(v, (types.BuiltinFunctionType, types.BuiltinMethodType, types.WrapperDescriptorType,
                      types.MethodDescriptorType, types.MethodWrapperType)):
        return "B:" + getattr(v, "__qualname__", repr(v))
    if isinstance(v, dict):
        items = [h(k, depth + 1, seen) + ":" + h(x, depth + 1, seen) for k, x in v.items()]
        if not all(type(k) in (str, int, bool, tuple) or k is None or isinstance(k, enum.Enum) for k in v):
            items.sort()  # keyed by classes/objects: order follows id() and is not stable
        return "{" + ",".join(items) + "}"
    if isinstance(v, (tuple, list)) and v and all(isinstance(x, type) for x in v):
        return t.__name__ + "~[" + ",".join(sorted(h(x, depth + 1, seen) for x in v)) + "]"  # often built from a set
    if isinstance(v, (list, tuple)):
        return t.__name__ + "[" + ",".join(h(x, depth + 1, seen) for x in v) + "]"
    if isinstance(v, (set, frozenset)):
        return t.__name__ + "{" + ",".join(sorted(h(x, depth + 1, seen) for x in v)) + "}"
    if isinstance(v, re.Pattern):
        return f"R:{v.pattern!r}:{v.flags}"
    if isinstance(v, functools.partial):
        return "PA:" + h(v.func, depth + 1, seen) + h(v.args, depth + 1, seen) + h(v.keywords, depth + 1, seen)
    state = {}
    try:
        d = object.__getattribute__(v, "__dict__")
        if isinstance(d, dict):
            state.update(d)
    except AttributeError:
        pass
    for klass in getattr(t, "__mro__", ()):
        slots = klass.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for sl in slots:
            if sl in ("__dict__", "__weakref__") or sl in state:
                continue
            try:
                state[sl] = getattr(v, sl)
            except Exception:  # noqa: BLE001
                state[sl] = "<unset>"
    if state:
        return "O:" + t.__module__ + "." + t.__qualname__ + h(state, depth + 1, seen)
    try:
        r = repr(v)
    except Exception:  # noqa: BLE001
        r = ""
    if r and " at 0x" not in r and len(r) < 2000:
        return "R:" + t.__module__ + "." + t.__qualname__ + ":" + r
    return "X:" + t.__module__ + "." + t.__qualname__


def digest(s):
    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def all_modules(root):
    mods = []
    base = os.path.join(root, "sqlglot")
    for d, dirs, files in os.walk(base, followlinks=True):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(d, f), root)[:-3]
                name = rel.replace(os.sep, ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                if name.endswith("__main__"):
                    continue
                mods.append(name)
    return mods


DICTS = {}
SHALLOW = [False, False]
VALUES = {}  # key -> value (for shallow / function info on demand)


def shallow(v):
    SHALLOW[0], SHALLOW[1] = True, False
    try:
        d = digest(h(v))
    finally:
        SHALLOW[0] = False
    return d, SHALLOW[1]


def funcinfo(v, root):
    f = v
    if isinstance(f, (staticmethod, classmethod)):
        f = f.__func__
    if not isinstance(f, types.FunctionType):
        return None
    c = f.__code__
    fn = os.path.realpath(c.co_filename)
    rootr = os.path.realpath(root) + os.sep
    if not fn.startswith(rootr):
        rootr = root.rstrip(os.sep) + os.sep
        if not c.co_filename.startswith(rootr):
            return None
        fn = c.co_filename
    last = max((l for _, _, l in c.co_lines() if l is not None), default=c.co_firstlineno)
    return [fn[len(rootr):], c.co_firstlineno, last, digest(f.__qualname__ + "|" + _code_sig(c))]


def entries(val):
    return {digest(h(k)): digest(h(v)) for k, v in val.items()}


def fingerprint(root, modules, key_order=None):
    """Hash `modules`. Class keys are assigned by walking `key_order` (all
    modules, fixed order) first, so they never depend on which subset we hash."""
    out = {}
    failed = []

    def load(names):
        res = []
        for name in names:
            try:
                res.append(importlib.import_module(name))
            except BaseException:  # noqa: BLE001
                failed.append(name)
        return res

    loaded = load(modules)
    keys = {}
    used_keys = {}

    def assign(cls, path_key):
        if id(cls) in keys:
            return
        key = f"{cls.__module__}:{cls.__qualname__}#{len(cls.__mro__)}"
        if used_keys.setdefault(key, id(cls)) != id(cls):
            key = path_key  # a second class with the same name (built dynamically)
        keys[id(cls)] = key
        for attr, val in list(cls.__dict__.items()):
            if isinstance(val, type) and str(getattr(val, "__module__", "")).startswith("sqlglot"):
                assign(val, f"{key}.{attr}")

    def walk_modules(mods, fn):
        for m in mods:
            for gname, val in list(vars(m).items()):
                if isinstance(val, type) and val.__module__ == m.__name__:
                    fn(val, f"{m.__name__}:{gname}")

    order = [sys.modules[n] for n in (key_order or []) if n in sys.modules] or loaded
    walk_modules(order, assign)
    walk_modules(loaded, assign)

    seen_cls = set()

    def do_class(cls, path_key):
        if id(cls) in seen_cls:
            return
        seen_cls.add(id(cls))
        assign(cls, path_key)
        key = keys[id(cls)]
        cname = f"{cls.__module__}.{cls.__qualname__}"
        for attr, val in list(cls.__dict__.items()):
            if attr in ("__dict__", "__weakref__", "__module__", "__doc__", "__annotations__",
                        "__static_attributes__", "__firstlineno__", "__qualname__"):
                continue
            kind = "F" if isinstance(val, (types.FunctionType, staticmethod, classmethod, property, type)) else "V"
            out[f"{key}.{attr}"] = (cname, digest(h(val)), kind)
            VALUES[f"{key}.{attr}"] = val
            if type(val) is dict:
                DICTS[f"{key}.{attr}"] = val
            if isinstance(val, type) and str(getattr(val, "__module__", "")).startswith("sqlglot"):
                do_class(val, f"{key}.{attr}")
        out[f"{key}.__mro__"] = (cname, digest(",".join(f"{c.__module__}.{c.__qualname__}" for c in cls.__mro__)), "F")

    for m in loaded:
        for gname, val in list(vars(m).items()):
            if gname.startswith("__") and gname not in ("__all__",):
                continue
            key = f"{m.__name__}:{gname}"
            if isinstance(val, type):
                if val.__module__ == m.__name__:
                    do_class(val, key)
                out[key] = ("", digest(h(val)), "T")
            elif isinstance(val, types.ModuleType):
                continue
            else:
                out[key] = ("", digest(h(val)), "V")
                VALUES[key] = val
                if type(val) is dict:
                    DICTS[key] = val
    return out, failed


def main():
    outp = sys.argv[1]
    root = os.getcwd()
    sys.path.insert(0, root)
    allm = all_modules(root)
    failed_all = []
    for name in allm:  # same import state as the baseline, whatever we hash
        try:
            importlib.import_module(name)
        except BaseException:  # noqa: BLE001
            failed_all.append(name)
    mods = sys.argv[2:] or allm
    fp, failed = fingerprint(root, mods, key_order=allm)
    failed = sorted(set(failed) | set(failed_all))
    ent = {}
    basep = os.environ.get("BR_FP_COMPARE")
    if basep == "all":
        ent = {k: entries(v) for k, v in DICTS.items()}
    elif basep:
        with open(basep) as f:
            base = json.load(f)["fp"]
        for k, v in DICTS.items():
            b = base.get(k)
            if b is not None and b[1] != fp[k][1]:
                ent[k] = entries(v)
    extra = {}
    if basep == "all":
        keys = list(VALUES)
    elif basep:
        keys = [k for k in VALUES if base.get(k) is None or base[k][1] != fp[k][1]]
    else:
        keys = []
    for k in keys:
        v = VALUES[k]
        fi = funcinfo(v, root)
        if fi is not None:
            extra[k] = {"fn": fi}
        elif fp[k][2] == "V" or fp[k][0] == "":
            sd, anon = shallow(v)
            extra[k] = {"sh": sd, "anon": anon}
    with open(outp, "w") as f:
        json.dump({"fp": fp, "failed": failed, "entries": ent, "extra": extra}, f)


if __name__ == "__main__":
    main()
