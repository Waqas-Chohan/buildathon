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
_FIXTURE_TOUCHES = {}  # relpath -> set of test ids

_real_open = builtins.open


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


def pytest_sessionfinish(session, exitstatus):
    out = {path: sorted(tests) for path, tests in _FIXTURE_TOUCHES.items()}
    out_path = os.environ.get("FIXTURE_TOUCHES_OUT", "/tmp/fixture_touches.json")
    with _real_open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f)
