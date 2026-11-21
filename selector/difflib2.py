"""Minimal unified-diff parser and applier (stdlib only)."""
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    def __init__(self):
        self.old = None  # path or None (/dev/null)
        self.new = None
        self.hunks = []  # list of (old_start, old_len, new_start, new_len, lines)
        self.binary = False
        self.rename = False
        self.mode_change = False

    @property
    def path(self):
        return self.new if self.new is not None else self.old

    def __repr__(self):
        return f"FilePatch({self.old}->{self.new}, {len(self.hunks)} hunks)"


def _strip(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


def parse(text):
    files = []
    cur = None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            i += 1
            continue
        if ln.startswith("rename from") or ln.startswith("rename to") or ln.startswith("copy "):
            cur.rename = True
        elif ln.startswith("old mode") or ln.startswith("new mode"):
            cur.mode_change = True
        elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
            cur.binary = True
        elif ln.startswith("new file mode"):
            pass
        elif ln.startswith("deleted file mode"):
            pass
        elif ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None:
                cur = FilePatch()
                files.append(cur)
            cur.old = _strip(ln[4:])
            cur.new = _strip(lines[i + 1][4:])
            i += 2
            continue
        elif ln.startswith("@@"):
            m = HUNK.match(ln)
            if not m:
                raise ValueError("bad hunk header")
            os_, ol, ns, nl = m.group(1), m.group(2), m.group(3), m.group(4)
            os_, ns = int(os_), int(ns)
            ol = 1 if ol is None else int(ol)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            co, cn = 0, 0
            while i < len(lines) and (co < ol or cn < nl):
                b = lines[i]
                if b.startswith("\\"):
                    i += 1
                    continue
                tag = b[:1] if b else " "
                if tag == " ":
                    co += 1; cn += 1
                elif tag == "-":
                    co += 1
                elif tag == "+":
                    cn += 1
                else:
                    raise ValueError("bad hunk line")
                body.append((tag, b[1:]))
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    return files


def apply(old_text, fp):
    """Apply a FilePatch's hunks to old_text; returns new text. Raises on mismatch."""
    if old_text is None:
        old_lines = []
        trailing = True
    else:
        trailing = old_text.endswith("\n")
        old_lines = old_text.split("\n")
        if trailing:
            old_lines.pop()
    out = []
    pos = 0  # index in old_lines
    for os_, ol, ns, nl, body in fp.hunks:
        start = os_ - 1 if ol > 0 else os_
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old_lines[pos:start])
        k = start
        for tag, txt in body:
            if tag == " ":
                if k >= len(old_lines) or old_lines[k] != txt:
                    raise ValueError(f"context mismatch at {k+1}")
                out.append(txt); k += 1
            elif tag == "-":
                if k >= len(old_lines) or old_lines[k] != txt:
                    raise ValueError(f"delete mismatch at {k+1}")
                k += 1
            else:
                out.append(txt)
        pos = k
    out.extend(old_lines[pos:])
    if fp.new is None:
        return None
    return "\n".join(out) + ("\n" if trailing or old_text is None else "")


def changed_old_lines(fp):
    """Old-file line numbers removed/modified, plus insertion anchors.

    Returns (removed:set[int], anchors:set[int]) where anchors are old line
    numbers immediately before and after a pure insertion point.
    """
    removed, anchors = set(), set()
    for os_, ol, ns, nl, body in fp.hunks:
        k = os_ if ol > 0 else os_ + 1
        for tag, _ in body:
            if tag == " ":
                k += 1
            elif tag == "-":
                removed.add(k); k += 1
            else:
                anchors.add(k - 1); anchors.add(k)
    return removed, anchors
