"""Generate synthetic patches that look like the sample commits.

usage: python mutants.py SQLGLOT_DIR OUTDIR N [--seed S]
Writes OUTDIR/commits/<id>.patch. Kinds: code (operator/constant/boolean/
argument edits in function bodies and class-level tables), comment, fixture,
multi. Used only to EVALUATE the selector; nothing derived from labels ships.
"""
import argparse, ast, difflib, os, random, re, string
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("src"); ap.add_argument("out"); ap.add_argument("n", type=int)
ap.add_argument("--seed", type=int, default=1)
a = ap.parse_args()
rng = random.Random(a.seed)
SRC = Path(a.src)

py_files = sorted(p for p in (SRC / "sqlglot").rglob("*.py") if "__pycache__" not in p.parts)
# weight like the samples: dialects/parsers/generators/optimizer/expressions/executor
fixtures = sorted((SRC / "tests" / "fixtures").rglob("*.sql"))

SWAPS = [
    (r"\bis not\b", "!="), (r"\bis None\b", "== None"), (r" is ", " == "), (r" == ", " != "), (r" != ", " == "),
    (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "), (r" and ", " or "), (r" or ", " and "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r" \+ 1\b", " + 2"), (r" - 1\b", " - 2"),
    (r"\bnot ", ""), (r"\[0\]", "[1]"), (r"\[-1\]", "[0]"), (r"seq_get\(args, (\d)\)", None),
]


def rel(p):
    return str(p.relative_to(SRC))


def make_patch(path, old, new):
    d = difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=3)
    body = "".join(d)
    if not body:
        return None
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + body


def mutate_code(text, tree):
    lines = text.split("\n")
    # candidate lines: inside functions or class-level assignments
    cand = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for st in ast.walk(node):
                if isinstance(st, ast.stmt) and st is not node:
                    cand.add(st.lineno)
        elif isinstance(node, ast.ClassDef):
            for st in node.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    for l in range(st.lineno, st.end_lineno + 1):
                        cand.add(l)
    cand = sorted(cand)
    rng.shuffle(cand)
    for ln in cand[:200]:
        s = lines[ln - 1]
        if s.strip().startswith("#") or '"""' in s:
            continue
        opts = SWAPS[:]
        rng.shuffle(opts)
        for pat, rep in opts:
            if re.search(pat, s):
                if rep is None:
                    m = re.search(pat, s)
                    new = s[: m.start(1)] + str((int(m.group(1)) + 1) % 4) + s[m.end(1):]
                else:
                    ms = list(re.finditer(pat, s))
                    m = rng.choice(ms)
                    new = s[: m.start()] + m.expand(rep) if "\\" in rep else s[: m.start()] + rep + s[m.end():]
                    if "\\" in rep:
                        new = s[: m.start()] + m.expand(rep) + s[m.end():]
                if new != s:
                    out = lines[:]
                    out[ln - 1] = new
                    nt = "\n".join(out)
                    try:
                        ast.parse(nt)
                    except SyntaxError:
                        continue
                    return nt
        # string literal tweak
        m = re.search(r'"([A-Z_]{3,})"', s)
        if m and rng.random() < 0.3:
            out = lines[:]
            out[ln - 1] = s[: m.start(1)] + m.group(1)[:-1] + s[m.end(1):]
            return "\n".join(out)
    return None


def mutate_comment(text, tree):
    lines = text.split("\n")
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.body]
    if not funcs:
        return None
    f = rng.choice(funcs)
    st = rng.choice([s for s in ast.walk(f) if isinstance(s, ast.stmt) and s is not f])
    ln = st.lineno
    indent = re.match(r"\s*", lines[ln - 1]).group(0)
    out = lines[: ln - 1] + [indent + "# " + rng.choice(["Cheap check first", "Keep order stable", "Note: see above", "TODO tidy"])] + lines[ln - 1:]
    return "\n".join(out)


def mutate_fixture(text):
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#") and not l.startswith("--")]
    if not idx:
        return None
    i = rng.choice(idx)
    s = lines[i]
    choice = rng.random()
    if choice < 0.5:
        toks = list(re.finditer(r"\b[a-z]\b|\b\d+\b", s))
        if not toks:
            return None
        m = rng.choice(toks)
        rep = rng.choice(string.ascii_lowercase) if m.group().isalpha() else str(int(m.group()) + 1)
        new = s[: m.start()] + rep + s[m.end():]
    elif choice < 0.75:
        new = s.replace(" AS ", " ", 1) if " AS " in s else s + " "
    else:
        new = s.replace(",", ", ", 1) if "," in s else s.upper()
    if new == s:
        return None
    lines[i] = new
    return "\n".join(lines)


def one():
    kind = rng.choices(["code", "comment", "fixture", "multi"], [0.6, 0.1, 0.2, 0.1])[0]
    parts = []
    k = 2 if kind == "multi" else 1
    for j in range(k):
        sub = kind if kind != "multi" else rng.choice(["code", "code", "fixture"])
        for _ in range(30):
            if sub == "fixture":
                p = rng.choice(fixtures)
                old = p.read_text()
                new = mutate_fixture(old)
            else:
                p = rng.choice(py_files)
                old = p.read_text()
                try:
                    tree = ast.parse(old)
                except SyntaxError:
                    continue
                new = mutate_code(old, tree) if sub == "code" else mutate_comment(old, tree)
            if new and new != old and not any(rel(p) == q for q, _ in parts):
                if old.endswith("\n") and not new.endswith("\n"):
                    new += "\n"
                pt = make_patch(rel(p), old, new)
                if pt:
                    parts.append((rel(p), pt))
                    break
    if len(parts) < k:
        return None, None
    return kind, "".join(pt for _, pt in parts)


os.makedirs(Path(a.out) / "commits", exist_ok=True)
made = 0
kinds = {}
while made < a.n:
    kind, patch = one()
    if not patch:
        continue
    cid = "m" + "".join(rng.choices(string.ascii_lowercase + string.digits, k=5))
    (Path(a.out) / "commits" / f"{cid}.patch").write_text(patch)
    kinds[cid] = kind
    made += 1
import json
json.dump(kinds, open(Path(a.out) / "kinds.json", "w"))
import hashlib
json.dump({p.stem: hashlib.sha1(p.read_bytes()).hexdigest()[:10] for p in sorted((Path(a.out) / "commits").glob("*.patch"))},
          open(Path(a.out) / "hashes.json", "w"))
print("made", made)
