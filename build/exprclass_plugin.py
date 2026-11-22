"""Build-time pytest plugin: which sqlglot classes does each test instantiate?

Expression classes in sqlglot/expressions/*.py are mostly pure declarations
(arg_types = {...}, is_var_len_args, _sql_names, ...). None of those lines
run at test time, so per-line coverage attributes them to nobody and a
mutation like `"constraint": False -> True` selected zero tests. The tests
that depend on a declaration are the tests that build instances of that
class (or of a subclass inheriting the declaration), so record exactly that.

The same holds one level up for dialects: a Generator/Parser/Tokenizer/
Dialect subclass's class-level flags (e.g. TeradataGenerator.SET_OP_LIMITS)
are read by base-class code, so a T-SQL test that *writes* Teradata SQL
depends on them while never executing a line in any Teradata file. Found by
mutation testing: SET_OP_LIMITS True -> False broke
test_tsql.py::TestTSQL::test_set_operation_branch_limits, outside every
coverage-derived selection. So the base __init__ of each of these
hierarchies is wrapped too.

Usage (from the pinned sqlglot checkout, with this directory on PYTHONPATH):
    EXPRMAP_DIR=/tmp/exprmap python -m pytest -p exprclass_plugin -n 2 tests
Each worker writes EXPRMAP_DIR/<worker>.json:
    {nodeid: ["module:QualName", ...], ...}
build/augment_exprclass.py turns those into index.json's "exprcls" field.
"""

import json
import os

import pytest

_current = [None]          # nodeid of the test currently running
_phase = ["call"]
_per_test: dict[str, set] = {}
_setup_by_class: dict[str, set] = {}
_item_class: dict[str, str] = {}


def _record(self):
    nid = _current[0]
    if nid is not None:
        cls = type(self)
        key = f"{cls.__module__}:{cls.__qualname__}"
        if _phase[0] == "setup" and nid in _item_class:
            _setup_by_class.setdefault(_item_class[nid], set()).add(key)
        _per_test.setdefault(nid, set()).add(key)


def _wrap(base):
    orig_init = base.__init__

    def tracking_init(self, *args, **kwargs):
        _record(self)
        orig_init(self, *args, **kwargs)

    base.__init__ = tracking_init


def pytest_configure(config):
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions import core
    from sqlglot.generator import Generator
    from sqlglot.optimizer.annotate_types import TypeAnnotator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for base in (core.Expr, Generator, Parser, Tokenizer, Dialect, TypeAnnotator):
        _wrap(base)


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is not None:
            _item_class[item.nodeid] = f"{item.module.__name__}.{cls.__qualname__}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _current[0] = item.nodeid
    _phase[0] = "setup"
    yield
    _phase[0] = "call"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _current[0] = item.nodeid
    _phase[0] = "call"
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _current[0] = item.nodeid
    _phase[0] = "teardown"
    yield
    _current[0] = None


def pytest_sessionfinish(session, exitstatus):
    out_dir = os.environ.get("EXPRMAP_DIR")
    if not out_dir:
        return
    os.makedirs(out_dir, exist_ok=True)
    # Instances built in a unittest class's setUpClass (a class-scoped
    # fixture that runs inside the first test's setup) are shared by every
    # test in that class.
    for nid, cls in _item_class.items():
        shared = _setup_by_class.get(cls)
        if shared:
            _per_test.setdefault(nid, set()).update(shared)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(out_dir, f"{worker}.json"), "w") as fh:
        json.dump({k: sorted(v) for k, v in _per_test.items()}, fh)
