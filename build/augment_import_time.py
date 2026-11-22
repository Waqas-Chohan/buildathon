"""Augment index.json with import-time / def-time line information.

Why this exists
---------------
build_index.py attributes every executed line to the test that was running
when it executed (coverage.py dynamic contexts). That is exact for code that
runs per call, but two kinds of *function-body* lines only ever run while a
module or class is being created, so their per-test attribution is either
empty (they ran during collection, before any test context existed) or a
handful of "whoever happened to import this dialect first" tests:

  1. Bodies of functions called at import time: metaclass / __init_subclass__
     hooks (dialects/dialect.py's _Dialect.__new__, tokens.py's
     Tokenizer.__init_subclass__), table builders (annotate_types.py's
     _build_coerces_to, simplify.py's _build_gen_dispatch), parser/generator
     builder helpers called inside class-body dicts, trie.new_trie, ...
     Whatever they compute is stored and read by *every* later user, but the
     selector used to treat those lines as precise, select the ~0-12
     attributed tests, and -- because "precise" -- skip every escalation,
     including the foundational-file select-all.

  2. Parameter lines of multi-line ``def`` signatures (default values are
     evaluated once, when the def statement runs). The tagger marks only the
     ``def name(`` line as header, so ``flag: bool = True,`` on its own line
     was treated as a precise body line with no tests.

This script measures (1) directly instead of guessing: it imports every
sqlglot module under sys.setprofile, records every function call whose
nearest module/class-body frame is at most MAX_DEPTH function frames away,
and the file(s) whose module/class body triggered it (the file that owns the
resulting state). It also records which lines actually executed during
import (sys.settrace line events), so only the import-time portion of each
function is flagged -- nested closures/lambdas that only run later keep
their exact per-test attribution. (2) is computed from the AST.

Output (added per file entry in index.json):
  "imp": [[func_lo, func_hi, [owner files...], [lines...]], ...]
  "sig": [[param_lo, param_hi, func_lo, func_hi], ...]

Usage:
  python build/augment_import_time.py /path/to/sqlglot-src index.json
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import pkgutil
import sys

MAX_DEPTH = 1  # 0 = called straight from a module/class body; 1 = one level below


def main() -> None:
    src = os.path.realpath(sys.argv[1]) + "/"
    index_path = sys.argv[2]
    sys.path.insert(0, src)

    def rel(fn: str) -> str | None:
        fn = os.path.realpath(fn)
        return fn[len(src):] if fn.startswith(src) else None

    newlocals = inspect.CO_NEWLOCALS
    funcs: dict[tuple[str, int], dict] = {}
    exec_lines: dict[tuple[str, int], set[int]] = {}

    def depth_and_owner(frame):
        d = 0
        f = frame.f_back
        while f is not None and (f.f_code.co_flags & newlocals):
            d += 1
            f = f.f_back
        if f is None:
            return None, None
        return d, rel(f.f_code.co_filename)

    def tracer(frame, event, arg):
        if event != "call":
            return None
        co = frame.f_code
        r = rel(co.co_filename)
        if r is None or not r.startswith("sqlglot/") or not (co.co_flags & newlocals):
            return None
        d, owner = depth_and_owner(frame)
        if d is None or d > MAX_DEPTH:
            return None
        key = (r, co.co_firstlineno)
        ent = funcs.setdefault(key, {"owners": set()})
        if owner:
            ent["owners"].add(owner)
        lines = exec_lines.setdefault(key, set())

        def local(fr, ev, a):
            if ev == "line" and fr.f_code is co:
                lines.add(fr.f_lineno)
            return local

        return local

    sys.settrace(tracer)
    try:
        # Trace from the very first import so the eagerly-imported core
        # (base Dialect/Tokenizer/Parser/Generator, expressions) is included.
        import sqlglot

        # Modules loaded by a bare `import sqlglot` are loaded by every test:
        # state they build at import is global, not per-dialect.
        eager = sorted(
            r for r in (rel(getattr(m, "__file__", "") or "") for m in list(sys.modules.values()))
            if r and r.startswith("sqlglot/")
        )

        for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            try:
                importlib.import_module(m.name)
            except Exception:  # noqa: BLE001
                pass
    finally:
        sys.settrace(None)

    index = json.load(open(index_path, encoding="utf-8"))
    files = index["files"]

    def tag(fi, line):
        for lo, hi, code, kidx in fi["tag_runs"]:
            if lo <= line <= hi:
                return code
        return 0

    trees: dict[str, ast.AST] = {}

    def tree_for(path):
        if path not in trees:
            trees[path] = ast.parse(open(src + path, encoding="utf-8").read())
        return trees[path]

    def func_span(path, firstline):
        for n in ast.walk(tree_for(path)):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = n.decorator_list[0].lineno if n.decorator_list else n.lineno
                if firstline in (n.lineno, start):
                    return n.lineno, n.end_lineno
            elif isinstance(n, ast.Lambda) and n.lineno == firstline:
                return n.lineno, n.end_lineno
        return firstline, firstline

    imp: dict[str, list] = {}
    for (path, firstline), ent in funcs.items():
        fi = files.get(path)
        if fi is None or fi["kind"] != "python":
            continue
        lo, hi = func_span(path, firstline)
        src_lines = open(src + path, encoding="utf-8").read().splitlines()
        body_lines = sorted(
            ln
            for ln in exec_lines.get((path, firstline), ())
            if tag(fi, ln) == 3
            # A line that builds a lambda at import AND runs that lambda's
            # body at call time (e.g. rename_func's single-line
            # "return lambda self, e: ...") already has exact per-test
            # attribution from the runtime calls -- don't escalate it.
            and not ("lambda" in src_lines[ln - 1] and fi["line_tests"].get(str(ln)))
        )
        if not body_lines:
            continue
        imp.setdefault(path, []).append([lo, hi, sorted(ent["owners"]), body_lines])

    sig: dict[str, list] = {}
    for path, fi in files.items():
        if fi["kind"] != "python" or not path.endswith(".py") or not os.path.exists(src + path):
            continue
        try:
            tree = tree_for(path)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
                p_lo, p_hi = n.lineno + 1, n.body[0].lineno - 1
                # The body's first statement may be a docstring; its first
                # line is where params end at the latest. Keep only lines
                # the tagger calls "body" -- everything else is already
                # handled as non-precise.
                p_hi = min(p_hi, n.end_lineno)
                lines = [ln for ln in range(p_lo, p_hi + 1) if tag(fi, ln) == 3]
                if lines:
                    sig.setdefault(path, []).append([min(lines), max(lines), n.lineno, n.end_lineno])

    # Multi-line string literals whose value is used at runtime (not bare
    # string statements such as docstrings): a "# comment" or blank line
    # added/removed inside one changes the value, so it is not trivial.
    strspans: dict[str, list] = {}
    for path, fi in files.items():
        if fi["kind"] != "python" or not os.path.exists(src + path):
            continue
        try:
            tree = tree_for(path)
        except SyntaxError:
            continue
        bare = {id(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Expr) and isinstance(n.value, (ast.Constant, ast.JoinedStr))}
        spans = set()
        for n in ast.walk(tree):
            if id(n) in bare or n.__class__ not in (ast.Constant, ast.JoinedStr):
                continue
            if isinstance(n, ast.Constant) and not isinstance(n.value, (str, bytes)):
                continue
            if n.end_lineno > n.lineno:
                spans.add((n.lineno, n.end_lineno))
        if spans:
            strspans[path] = sorted(spans)

    index["eager_files"] = eager
    for path, fi in files.items():
        fi.pop("imp", None)
        fi.pop("sig", None)
        fi.pop("strspans", None)
        if path in strspans:
            fi["strspans"] = [list(s) for s in strspans[path]]
        if path in imp:
            fi["imp"] = sorted(imp[path])
        if path in sig:
            fi["sig"] = sorted(sig[path])

    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))
    print(
        f"imp: {sum(len(v) for v in imp.values())} functions in {len(imp)} files; "
        f"sig: {sum(len(v) for v in sig.values())} signatures in {len(sig)} files",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
