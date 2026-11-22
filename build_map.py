"""Run the sqlglot suite once with per-test coverage and write maps/ (run via make_maps.sh)."""
import gzip, json, os, re, subprocess, sys
from pathlib import Path
import coverage

SRC = "/opt/sqlglot"


def main(tests_json, out):
    ids = [t["id"] for t in json.load(open(tests_json))["tests"]]
    pos = {t: i for i, t in enumerate(ids)}
    raw = "/tmp/raw.json"
    env = dict(os.environ, MAP_RAW=raw)
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    for old in Path("/tmp").glob("raw.json.*"):
        old.unlink()
    try:
        subprocess.run([sys.executable, "-m", "pytest", "tests", "-n", "4", "-p", "mapplugin", "--cov=sqlglot",
                        "--cov-context=test", "--cov-report=", "-q", "-p", "no:cacheprovider"],
                       env=env, cwd=SRC, timeout=int(os.environ.get("MAP_TIMEOUT", "420")))
    except subprocess.TimeoutExpired:
        (outp / "INVALID").write_text("timeout")
        print("map build timed out; maps marked INVALID", file=sys.stderr)
        return
    pcache = {}

    def prefix_mask(prefix):
        m = pcache.get(prefix)
        if m is None:
            m = 0
            for i, t in enumerate(ids):
                if t == prefix or t.startswith(prefix + "::") or t.startswith(prefix + "["):
                    m |= 1 << i
            pcache[prefix] = m
        return m

    def key_mask(kind, key):
        if kind == "T":
            i = pos.get(key)
            return 1 << i if i is not None else 0
        return prefix_mask(key)

    def ctx_mask(ctx):
        nodeid, _, when = ctx.rpartition("|")
        if not nodeid.startswith("tests/"):
            return 0
        if when == "run":
            return key_mask("T", nodeid)
        return prefix_mask(nodeid.rsplit("::", 1)[0])

    (outp / "lines").mkdir(parents=True, exist_ok=True)
    data = coverage.CoverageData(basename=os.path.join(SRC, ".coverage"))
    data.read()
    ran = {c.rpartition("|")[0] for c in data.measured_contexts() if c.endswith("|run")}
    ratio = len(ran & set(ids)) / max(1, len(ids))
    print("tests with coverage:", round(ratio, 3), file=sys.stderr)
    if ratio < 0.97:
        (outp / "INVALID").write_text(f"coverage ratio {ratio}")
        print("incomplete coverage; maps marked INVALID", file=sys.stderr)
        return
    touch = {}
    for fname in data.measured_files():
        rel = os.path.relpath(fname, SRC)
        if not rel.startswith("sqlglot/"):
            continue
        sets, setidx, lines, total = [], {}, {}, 0
        cmc = {}
        for ln, ctxs in data.contexts_by_lineno(fname).items():
            m = 0
            for c in ctxs:
                v = cmc.get(c)
                if v is None:
                    v = cmc[c] = ctx_mask(c)
                m |= v
            if not m:
                continue
            total |= m
            i = setidx.get(m)
            if i is None:
                i = setidx[m] = len(sets)
                sets.append(format(m, "x"))
            lines[str(ln)] = i
        touch[rel] = format(total, "x")
        blob = json.dumps({"sets": sets, "lines": lines}, separators=(",", ":")).encode()
        (outp / "lines" / (rel.replace("/", "__") + ".json.gz")).write_bytes(gzip.compress(blob, 9))

    r = {"fixtures": {}, "used": {}}
    for fp in Path("/tmp").glob("raw.json.*"):
        part = json.load(open(fp))
        for sect in ("fixtures", "used"):
            for k, v in part[sect].items():
                cur = r[sect].setdefault(k, [])
                cur.extend(x for x in v if x not in cur)

    def fold(d):
        res = {}
        for k, keys in d.items():
            m = 0
            for kind, key in keys:
                m |= key_mask(kind, key)
            res[k] = format(m, "x")
        return res

    classmods = fold(r["used"])
    for k, h in list(classmods.items()):          # module-level union
        mod = k.split(":", 1)[0]
        classmods[mod] = format(int(classmods.get(mod, "0"), 16) | int(h, 16), "x")
    fixtures = fold(r["fixtures"])

    # test files imported by other test files (e.g. Validator in test_dialect.py)
    files = sorted({t.split("::")[0] for t in ids})
    mod_of = {f[:-3].replace("/", "."): f for f in files}
    imp_re = re.compile(r"^\s*(?:from\s+(tests[\w.]*)\s+import|import\s+(tests[\w.]*))", re.M)
    importers = {}
    for f in files:
        for a, b in imp_re.findall(Path(SRC, f).read_text()):
            tgt = mod_of.get(a or b)
            if tgt and tgt != f:
                importers.setdefault(tgt, []).append(f)

    (outp / "index.json").write_text(json.dumps(
        {"ids": ids, "touch": touch, "classmods": classmods, "fixtures": fixtures,
         "importers": importers}, separators=(",", ":")))
    print("seen fixtures:", len(fixtures), "class keys:", len(classmods), file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
