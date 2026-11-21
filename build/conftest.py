"""Build-time instrumentation only. This file is NOT shipped in the final
run-time image — it exists purely so the indexer can learn, during the
baseline full-suite run, which tests read which non-Python fixture files
(coverage.py's line tracing already tells us everything about .py files).
"""
import builtins
import concurrent.futures
import io
import json
import os
import pickle
import unittest

import pytest


# -- Build-time-only patch -------------------------------------------------
# A couple of test classes (TestOptimizer, TestExecutor) farm the actual
# work out to a ProcessPoolExecutor (spawn context). coverage.py's per-test
# dynamic context tracking cannot see across that process boundary at all,
# so every line executed inside those worker processes would silently show
# as "covered by no one" -- exactly the optimizer files our patches touch
# most. We only need *coverage attribution*, not real parallelism, for this
# indexing run, so replace the pool with a synchronous in-process shim that
# runs the submitted work directly under the current test's context.
class _SyncExecutor:
    def __init__(self, *args, **kwargs):
        initializer = kwargs.get("initializer")
        initargs = kwargs.get("initargs", ())
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            # A real ProcessPoolExecutor pickles fn/args to ship them to the
            # worker, and pickles the result to ship it back. sqlglot's
            # Expression defines a custom __reduce__ that routes through
            # serde.dump/load -- real, test-relevant code that must stay
            # visible to coverage. Do the same round trip in-process so
            # coverage sees it without actually paying for a new process.
            payload = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fn2, args2, kwargs2 = payload
            result = fn2(*args2, **kwargs2)
            result = pickle.loads(pickle.dumps(result))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future

    def map(self, fn, *iterables, **kwargs):
        return list(map(fn, *iterables))

    def shutdown(self, wait=True, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


concurrent.futures.ProcessPoolExecutor = _SyncExecutor

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_CURRENT_TEST = [None]
_FIXTURE_TOUCHES = {}  # relpath -> set of (test id OR "CLASSWIDE::<key>") markers
_CLASS_TO_TESTS: dict[str, set] = {}  # "module.qualname" -> set of nodeids in that class

_real_open = builtins.open

_CLASSWIDE_PREFIX = "CLASSWIDE::"


def _tracking_open(file, mode="r", *args, **kwargs):
    try:
        path = os.fspath(file)
    except TypeError:
        path = None
    if path is not None and _CURRENT_TEST[0] is not None and "r" in mode:
        abspath = os.path.abspath(path)
        if abspath.startswith(_REPO_ROOT + os.sep) and not abspath.endswith(".py"):
            rel = os.path.relpath(abspath, _REPO_ROOT).replace(os.sep, "/")
            _FIXTURE_TOUCHES.setdefault(rel, set()).add(_CURRENT_TEST[0])
    return _real_open(file, mode, *args, **kwargs)


builtins.open = _tracking_open
io.open = _tracking_open


@pytest.fixture(autouse=True)
def _track_current_test(request):
    _CURRENT_TEST[0] = request.node.nodeid
    try:
        yield
    finally:
        _CURRENT_TEST[0] = None


# -- setUpClass fixture-file blind spot ------------------------------------
# unittest.TestCase.setUpClass runs ONCE per test class, BEFORE our own
# function-scoped `_track_current_test` autouse fixture has set
# _CURRENT_TEST[0] for the class's first test. Several real test classes
# (TestExecutor above all) load non-Python fixture files there -- TPC-H/
# TPC-DS .csv.gz tables and their .sql fixture -- shared by every test
# method in the class. Without this, those opens happen while
# _CURRENT_TEST[0] is still None, so _tracking_open silently drops them:
# the fixture files never make it into the index at all, and a hidden
# commit that edits one of them would get ZERO tests selected -- a
# guaranteed miss for every test that actually depends on it. (Confirmed:
# tests/fixtures/optimizer/tpc-ds/*.csv.gz don't appear in the index at
# all, and tpc-ds.sql/tpc-h.sql are each attributed to only the one
# test_optimizer.py test that happens to read them directly inside its own
# test body, missing test_executor.py's test_execute_tpcds/test_execute_tpch
# entirely.)
#
# Fix: wrap every collected TestCase subclass's own setUpClass (only if it
# defines one) so that, for its duration, _CURRENT_TEST[0] is a classwide
# marker instead of None. At session end, expand that marker into every
# test nodeid that belongs to the class -- safe (attributes to the real
# consumers, never fewer) even though it's broader than per-test precision
# would be, since a whole TestExecutor-sized class is still tiny next to
# the 300s-per-miss cost of getting it wrong.
def _wrap_setupclass(cls: type) -> None:
    orig = cls.__dict__.get("setUpClass")
    if orig is None or getattr(orig, "_blast_radius_wrapped", False):
        return
    key = f"{cls.__module__}.{cls.__qualname__}"

    def wrapper(cls_, *a, **kw):
        prev = _CURRENT_TEST[0]
        _CURRENT_TEST[0] = _CLASSWIDE_PREFIX + key
        try:
            return orig.__func__(cls_, *a, **kw)
        finally:
            _CURRENT_TEST[0] = prev

    wrapper._blast_radius_wrapped = True
    cls.setUpClass = classmethod(wrapper)


# pytest's unittest integration reads cls.setUpClass off the class and
# wraps it into its own fixture *inside* UnitTestCase.collect(). By the
# time pytest_collection_modifyitems fires (after collection is fully
# done), that reference has already been captured, so patching there is
# too late -- confirmed empirically (0 extra tests attributed). collect()
# is called from collect_one_node() right after the pytest_collectstart
# hook fires for that same collector, so patching here runs first.
def pytest_collectstart(collector):
    cls = getattr(collector, "obj", None)
    if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
        _wrap_setupclass(cls)


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None:
            continue
        key = f"{cls.__module__}.{cls.__qualname__}"
        _CLASS_TO_TESTS.setdefault(key, set()).add(item.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = {}
    for path, markers in _FIXTURE_TOUCHES.items():
        expanded = set()
        for m in markers:
            if m.startswith(_CLASSWIDE_PREFIX):
                expanded.update(_CLASS_TO_TESTS.get(m[len(_CLASSWIDE_PREFIX):], set()))
            else:
                expanded.add(m)
        out[path] = sorted(expanded)
    out_path = os.environ.get("FIXTURE_TOUCHES_OUT", "/tmp/fixture_touches.json")
    with _real_open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f)
