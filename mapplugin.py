"""pytest plugin: records which fixtures and which sqlglot classes each test touches."""
import builtins, io, json, os
import pytest

ROOT = os.getcwd()
CUR = [None]            # ("T", nodeid) in a test body, ("C", class/file prefix) in setup/teardown, ("F", file) at collection
FIX = {}                # fixture path -> set of CUR keys
USED = {}               # "sqlglot/x.py:Qualname" -> set of CUR keys
_seen = set()


def _rel_fixture(p):
    try:
        r = os.path.relpath(os.path.abspath(os.fspath(p)), ROOT)
    except Exception:
        return None
    return r if r.startswith("tests/fixtures") else None


_real_open = builtins.open


def _open(file, *a, **k):
    cur = CUR[0]
    if cur is not None and isinstance(file, (str, os.PathLike)):
        r = _rel_fixture(file)
        if r:
            FIX.setdefault(r, set()).add(cur)
    return _real_open(file, *a, **k)


builtins.open = _open
io.open = _open


def _note(cls):
    cur = CUR[0]
    if cur is None or (cur, cls) in _seen:
        return
    _seen.add((cur, cls))
    for c in cls.__mro__:
        mod = getattr(c, "__module__", "")
        if mod.startswith("sqlglot"):
            USED.setdefault(mod.replace(".", "/") + ".py:" + c.__qualname__, set()).add(cur)


def _install():
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    from sqlglot.tokens import Tokenizer
    from sqlglot.expressions import Expression
    for base in (Dialect, Parser, Generator, Tokenizer, Expression):
        orig = base.__init__

        def make(orig):
            def init(self, *a, **k):
                _note(type(self))
                return orig(self, *a, **k)
            return init
        try:
            base.__init__ = make(orig)
        except Exception as e:  # pragma: no cover
            print("cannot wrap", base, e)


def _cls(item):
    return item.nodeid.rsplit("::", 1)[0]


def pytest_sessionstart(session):
    _install()


def pytest_collectstart(collector):
    nid = getattr(collector, "nodeid", "")
    if nid.endswith(".py"):
        CUR[0] = ("F", nid)


def pytest_collection_finish(session):
    CUR[0] = None


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    CUR[0] = ("C", _cls(item))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    CUR[0] = ("T", item.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    CUR[0] = ("C", _cls(item))


def pytest_runtest_logfinish(nodeid, location):
    CUR[0] = None


def pytest_sessionfinish(session):
    out = os.environ.get("MAP_RAW", "/tmp/raw.json") + "." + str(os.getpid())
    ser = lambda d: {k: sorted([list(x) for x in v]) for k, v in d.items()}
    with _real_open(out, "w") as f:
        json.dump({"fixtures": ser(FIX), "used": ser(USED)}, f)
