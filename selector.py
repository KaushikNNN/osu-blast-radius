"""Blast-radius selector: map a patch to the tests it can break, then pack them onto workers.

Needs (all built from the pinned commit): /opt/sqlglot (source), maps/ (per-line test coverage,
class-usage and fixture maps made once by tools/make_maps.sh).  Never imports sqlglot.
"""
import ast, bisect, gzip, json, math, re, sys
from pathlib import Path

DATA = Path("/data")
APP = Path(__file__).resolve().parent
MAPS = APP / "maps"  # filled during docker build
SRC = Path("/opt/sqlglot")
MAX_WORKERS, WORKER_MS = 8, 2000
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
NOTHING_EXT = (".md", ".rst", ".txt", ".png", ".jpg", ".svg", ".gif")
NOTHING_DIRS = ("docs/", ".github/", "posts/", "benchmarks/", "sqlglotrs/")  # verify against sample patches


def log(*a):
    print(*a, file=sys.stderr)


# ------------------------------------------------------------------ patch parsing
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_patch(text):
    files, cur = [], None
    lines = text.split("\n")
    i, n = 0, len(lines)

    def start():
        c = {"old": None, "new": None, "hunks": [], "flags": set(), "saw_old": False}
        files.append(c)
        return c

    while i < n:
        l = lines[i]
        if l.startswith("diff --git "):
            cur = start()
            m = re.match(r"diff --git (\S+) (\S+)$", l)
            if m:
                cur["old"], cur["new"] = _strip(m.group(1)), _strip(m.group(2))
            else:
                cur["flags"].add("odd")
        elif l.startswith("--- "):
            if cur is None or cur["saw_old"]:
                cur = start()
            cur["old"], cur["saw_old"] = _strip(l[4:]), True
        elif l.startswith("+++ ") and cur is not None:
            cur["new"] = _strip(l[4:])
        elif l.startswith(("rename ", "copy ")):
            cur["flags"].add("rename")
        elif l.startswith(("Binary files", "GIT binary patch", "old mode", "new mode")):
            cur["flags"].add("binary")
        elif l.startswith("@@") and cur is not None:
            m = HUNK.match(l)
            if not m:
                cur["flags"].add("odd")
            else:
                oc = int(m.group(2)) if m.group(2) is not None else 1
                nc = int(m.group(4)) if m.group(4) is not None else 1
                body, r, a = [], 0, 0
                while i + 1 < n and (r < oc or a < nc):
                    i += 1
                    t = lines[i]
                    tag = t[:1]
                    if tag == "\\":
                        continue
                    if t == "":
                        tag = " "
                    if tag == " ":
                        r += 1; a += 1; body.append((" ", t[1:]))
                    elif tag == "-":
                        r += 1; body.append(("-", t[1:]))
                    elif tag == "+":
                        a += 1; body.append(("+", t[1:]))
                    else:
                        break
                cur["hunks"].append({"os": int(m.group(1)), "oc": oc, "ns": int(m.group(3)), "body": body})
        i += 1
    return files


def apply_hunks(old_lines, hunks):
    out, pos = [], 0
    for h in hunks:
        start = h["os"] if h["oc"] == 0 else h["os"] - 1
        out += old_lines[pos:start]
        pos = start
        for tag, text in h["body"]:
            if tag == "+":
                out.append(text)
                continue
            if pos >= len(old_lines) or old_lines[pos] != text:
                raise ValueError("patch does not match source")
            if tag == " ":
                out.append(text)
            pos += 1
    out += old_lines[pos:]
    return out


def changed_lines(hunks):
    old, new = set(), set()
    for h in hunks:
        o, w = h["os"], h["ns"]
        for tag, _ in h["body"]:
            if tag == " ":
                o += 1; w += 1
            elif tag == "-":
                old.add(o); o += 1
            else:
                new.add(w); w += 1
    return sorted(old), sorted(new)


# ------------------------------------------------------------------ structural diff
def index_scopes(tree):
    """qualname -> (node, parent); plus the non-def statements owned by each scope."""
    idx, own, counts = {"": (tree, None)}, {}, {}

    def walk(stmts, scope):
        for s in stmts:
            if isinstance(s, DEFS):
                base = f"{scope}.{s.name}" if scope else s.name
                k = counts.get(base, 0)
                counts[base] = k + 1
                q = base if k == 0 else f"{base}#{k}"
                idx[q] = (s, scope)
                walk(s.body, q)
            else:
                own.setdefault(scope, []).append(s)
                for f in ("body", "orelse", "finalbody"):
                    sub = getattr(s, f, None)
                    if isinstance(sub, list):
                        walk(sub, scope)
                for h in getattr(s, "handlers", []):
                    walk(h.body, scope)

    walk(tree.body, "")
    return idx, own


def _span(node):
    lo = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return lo, node.end_lineno


def _touched(node, changed):
    lo, hi = _span(node)
    j = bisect.bisect_left(changed, lo)
    return j < len(changed) and changed[j] <= hi


def _class_own(node, stmts):
    head = [ast.dump(x) for x in node.bases + node.keywords + node.decorator_list]
    return head, [ast.dump(s) for s in stmts]


def analyze(old_src, new_src, old_ch, new_ch):
    ot, nt = ast.parse(old_src), ast.parse(new_src)
    oi, oown = index_scopes(ot)
    ni, nown = index_scopes(nt)
    res = {"modified": {}, "removed": {}, "added": set(), "scopes": set(),
           "classes": {q for q, (nd, _) in oi.items() if isinstance(nd, ast.ClassDef)}}
    for q, (on, _) in oi.items():
        if q == "":
            continue
        if q not in ni:
            res["removed"][q] = _span(on)
            continue
        nn = ni[q][0]
        if not (_touched(on, old_ch) or _touched(nn, new_ch)):
            continue
        if isinstance(on, ast.ClassDef) and isinstance(nn, ast.ClassDef):
            if _class_own(on, oown.get(q, [])) != _class_own(nn, nown.get(q, [])):
                res["scopes"].add(q)
        elif type(on) is not type(nn) or ast.dump(on) != ast.dump(nn):
            res["modified"][q] = _span(on)
    res["added"] = {q for q in ni if q and q not in oi}
    if [ast.dump(s) for s in oown.get("", [])] != [ast.dump(s) for s in nown.get("", [])]:
        res["scopes"].add("")
    return res


def parent_of(q):
    return q.rsplit(".", 1)[0] if "." in q else ""


# ------------------------------------------------------------------ the selector
class Ctx:
    def __init__(self, tests):
        self.idx = json.loads((MAPS / "index.json").read_text())
        self.ids = self.idx["ids"]
        self.num_of = {t["id"]: t["number"] for t in tests}
        self.all_numbers = sorted(t["number"] for t in tests)
        self.slot_num = [self.num_of.get(t) for t in self.ids]
        self.pcache, self.lcache = {}, {}
        self.hexmap = lambda d, k: int(d.get(k, "0"), 16)

    def prefix(self, p):
        m = self.pcache.get(p)
        if m is None:
            m = 0
            for i, t in enumerate(self.ids):
                if t == p or t.startswith(p + "::") or t.startswith(p + "["):
                    m |= 1 << i
            self.pcache[p] = m
        return m

    def lines(self, module):
        if module not in self.lcache:
            p = MAPS / "lines" / (module.replace("/", "__") + ".json.gz")
            if not p.exists():
                self.lcache[module] = {}
            else:
                d = json.loads(gzip.decompress(p.read_bytes()))
                sets = [int(h, 16) for h in d["sets"]]
                self.lcache[module] = {int(k): sets[v] for k, v in d["lines"].items()}
        return self.lcache[module]

    def numbers(self, mask):
        out, i = [], 0
        while mask:
            low = mask & -mask
            i = low.bit_length() - 1
            n = self.slot_num[i] if i < len(self.slot_num) else None
            if n is not None:
                out.append(n)
            mask ^= low
        return out


ALL = None  # sentinel: run everything


def select_source(ctx, mod, f):
    if f["old"] is None or f["new"] is None or f["old"] != f["new"]:
        return ALL
    old = (SRC / mod).read_text()
    old_l = old.split("\n")
    new_l = apply_hunks(old_l, f["hunks"])
    oc, nc = changed_lines(f["hunks"])
    res = analyze(old, "\n".join(new_l), oc, nc)
    mask = 0
    lines = None
    for lo, hi in list(res["modified"].values()) + list(res["removed"].values()):
        lines = lines if lines is not None else ctx.lines(mod)
        for l in range(lo, hi + 1):
            mask |= lines.get(l, 0)
    scopes = set(res["scopes"])
    for q in list(res["added"]) + list(res["removed"]):
        scopes.add(parent_of(q))
    touch = ctx.hexmap(ctx.idx["touch"], mod)
    for q in scopes:
        if q == "":
            mask |= touch | ctx.hexmap(ctx.idx["classmods"], mod)
        elif q in res["classes"]:
            key = f"{mod}:{q}"
            mask |= ctx.hexmap(ctx.idx["classmods"], key) if key in ctx.idx["classmods"] else touch
        # a non-class parent is a function: already covered by "modified"
    return mask


def select_testfile(ctx, path, f):
    if f["old"] is None:
        return 0                                  # new test file: its tests have no numbers
    if f["new"] is None or f["old"] != f["new"]:
        return ctx.prefix(path)
    old = (SRC / path).read_text()
    new_l = apply_hunks(old.split("\n"), f["hunks"])
    oc, nc = changed_lines(f["hunks"])
    res = analyze(old, "\n".join(new_l), oc, nc)
    mask, helper = 0, False

    def test_prefix(q):
        parts = [p.split("#")[0] for p in q.split(".")]
        for j, p in enumerate(parts):
            if p.startswith("test"):
                return path + "::" + "::".join(parts[: j + 1])
        return None

    for q in list(res["modified"]) + list(res["removed"]):
        tp = test_prefix(q)
        if tp:
            mask |= ctx.prefix(tp)
        else:
            helper = True
            par = parent_of(q)
            mask |= ctx.prefix(path + "::" + par.replace(".", "::")) if par else ctx.prefix(path)
    for q in res["added"]:
        if not test_prefix(q):
            helper = True
            par = parent_of(q)
            mask |= ctx.prefix(path + "::" + par.replace(".", "::")) if par else ctx.prefix(path)
    for q in res["scopes"]:
        helper = True
        mask |= ctx.prefix(path + "::" + q.replace(".", "::")) if q else ctx.prefix(path)
    if helper:
        for imp in ctx.idx["importers"].get(path, []):
            mask |= ctx.prefix(imp)
    return mask


def select_file(ctx, f):
    p = f["new"] or f["old"]
    if p is None:
        return ALL
    if p.startswith("sqlglot/") and p.endswith(".py"):
        return ALL if f["flags"] else select_source(ctx, p, f)
    if p.startswith("sqlglot/"):
        return 0 if p.endswith(".typed") else ALL
    if p.startswith("tests/fixtures/"):
        paths = {x for x in (f["old"], f["new"]) if x}
        mask = 0
        for x in paths:
            mask |= ctx.hexmap(ctx.idx["fixtures"], x)
        return mask
    if p.startswith("tests/"):
        name = p.rsplit("/", 1)[-1]
        if p.endswith(".py") and name.startswith("test_") and not f["flags"]:
            return select_testfile(ctx, p, f)
        return ALL
    if p.endswith(NOTHING_EXT) or p.startswith(NOTHING_DIRS):
        return 0
    return ALL                                    # pyproject, setup, Makefile, conftest, unknown


def select_commit(ctx, text):
    files = parse_patch(text)
    if not files:
        return ALL
    mask = 0
    for f in files:
        try:
            m = select_file(ctx, f)
        except Exception as e:                    # fail safe: unknown => everything
            log("fallback to ALL:", f.get("new") or f.get("old"), repr(e))
            m = ALL
        if m is ALL:
            return ALL
        mask |= m
    return mask


# ------------------------------------------------------------------ packing
def schedule(nums, dur):
    if not nums:
        return []
    nums = sorted(nums, key=lambda n: -dur[n])
    best = None
    for k in range(1, min(MAX_WORKERS, len(nums)) + 1):
        loads, ws = [0] * k, [[] for _ in range(k)]
        for n in nums:
            j = loads.index(min(loads))
            ws[j].append(n)
            loads[j] += dur[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    try:
        if (MAPS / "INVALID").exists():
            raise RuntimeError("maps invalid")
        ctx = Ctx(tests)
    except Exception as e:
        log("no usable maps, running everything:", repr(e))
        ctx = None
    everything = schedule(sorted(dur), dur)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        if ctx is None:
            print(json.dumps({"commit": patch.stem, "workers": everything}))
            continue
        try:
            mask = select_commit(ctx, text)
        except Exception as e:
            log("fallback to ALL:", repr(e))
            mask = ALL
        nums = ctx.all_numbers if mask is ALL else ctx.numbers(mask)
        print(json.dumps({"commit": patch.stem, "workers": schedule(nums, dur)}))
        log(patch.stem, "ALL" if mask is ALL else len(nums), "tests")


if __name__ == "__main__":
    main()
