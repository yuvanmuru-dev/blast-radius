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
import hashlib
import importlib
import json
import os
import re
import sys
import types

MAXDEPTH = 8


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
    if hasattr(v, "__dict__") and not isinstance(v, type):
        try:
            return "O:" + t.__module__ + "." + t.__qualname__ + h(dict(vars(v)), depth + 1, seen)
        except TypeError:
            pass
    slots = getattr(t, "__slots__", None)
    if slots:
        if isinstance(slots, str):
            slots = (slots,)
        return "S:" + t.__qualname__ + "(" + ",".join(h(getattr(v, s, None), depth + 1, seen) for s in slots) + ")"
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


def fingerprint(root, modules):
    out = {}
    failed = []
    loaded = []
    for name in modules:
        try:
            loaded.append(importlib.import_module(name))
        except BaseException as e:  # noqa: BLE001
            failed.append(name)
    seen_cls = set()

    def do_class(cls, key):
        if id(cls) in seen_cls:
            return
        seen_cls.add(id(cls))
        cname = f"{cls.__module__}.{cls.__qualname__}"
        for attr, val in list(cls.__dict__.items()):
            if attr in ("__dict__", "__weakref__", "__module__", "__doc__", "__annotations__",
                        "__static_attributes__", "__firstlineno__", "__qualname__"):
                continue
            kind = "F" if isinstance(val, (types.FunctionType, staticmethod, classmethod, property, type)) else "V"
            out[f"{key}.{attr}"] = (cname, digest(h(val)), kind)
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
    return out, failed


def main():
    outp = sys.argv[1]
    root = os.getcwd()
    sys.path.insert(0, root)
    mods = sys.argv[2:] or all_modules(root)
    fp, failed = fingerprint(root, mods)
    with open(outp, "w") as f:
        json.dump({"fp": fp, "failed": failed}, f)


if __name__ == "__main__":
    main()
