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

    # Every function-body line executed while importing, at ANY depth: e.g.
    # a dialect module parsing SQL strings during class creation runs deep
    # parser code whose result is cached in class constants. Mutation
    # testing found exactly this (parser.py:4653 _parse_into, run while
    # Athena's classes are created, broke Athena tests that never call it
    # at runtime), so such lines can't use per-test attribution.
    all_import_lines: set[tuple[str, int]] = set()

    def deep_local_for(r):
        def deep_local(fr, ev, a):
            if ev == "line":
                all_import_lines.add((r, fr.f_lineno))
            return deep_local
        return deep_local

    def tracer(frame, event, arg):
        if event != "call":
            return None
        co = frame.f_code
        r = rel(co.co_filename)
        if r is None or not r.startswith("sqlglot/") or not (co.co_flags & newlocals):
            return None
        d, owner = depth_and_owner(frame)
        if d is None or d > MAX_DEPTH:
            return deep_local_for(r) if d is not None else None
        key = (r, co.co_firstlineno)
        ent = funcs.setdefault(key, {"owners": set()})
        if owner:
            ent["owners"].add(owner)
        lines = exec_lines.setdefault(key, set())

        def local(fr, ev, a):
            if ev == "line":
                all_import_lines.add((r, fr.f_lineno))
                if fr.f_code is co:
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

    # Lazy runtime caches: run a small workload twice per dialect; lines that
    # execute on the first run but not on an identical second run are
    # first-use initialisation whose result is cached and reused (e.g.
    # generator.py's _DISPATCH_CACHE filled by _build_dispatch). Like
    # import-time code, coverage credits them only to the first test.
    lazy: dict[str, set[int]] = {}
    try:
        from sqlglot import exp as _exp
        from sqlglot.dialects.dialect import Dialect as _D
        from sqlglot.executor import execute as _execute
        from sqlglot.optimizer import optimize as _optimize

        def workload(d):
            sqlglot.transpile("SELECT a, SUM(b) AS s FROM t WHERE c > 1 GROUP BY a", read=d, write=d)
            _optimize(sqlglot.parse_one("SELECT a FROM t WHERE b = 1", read=d),
                      schema={"t": {"a": "INT", "b": "INT"}}, dialect=d)

        def traced_lines(fn):
            seen: set[tuple[str, int]] = set()

            def gt(frame, event, arg):
                r = rel(frame.f_code.co_filename)
                if r is None or not r.startswith("sqlglot/"):
                    return None

                def lt(fr, ev, a):
                    if ev == "line":
                        seen.add((r, fr.f_lineno))
                    return lt

                return lt

            sys.settrace(gt)
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
            finally:
                sys.settrace(None)
            return seen

        names = [""] + sorted(k for k in _D.classes if k)
        for d in names:
            first = traced_lines(lambda: workload(d))
            second = traced_lines(lambda: workload(d))
            for r, ln in first - second:
                lazy.setdefault(r, set()).add(ln)
        first = traced_lines(lambda: _execute("SELECT 1 AS x"))
        second = traced_lines(lambda: _execute("SELECT 1 AS x"))
        for r, ln in first - second:
            lazy.setdefault(r, set()).add(ln)
    except Exception as e:  # noqa: BLE001
        print(f"lazy-cache detection skipped: {e!r}", file=sys.stderr)

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

    _src_cache: dict[str, list] = {}
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

    # impall: body lines executed at import (any depth), minus lambda lines
    # whose runtime calls already give them exact attribution.
    impall: dict[str, list] = {}
    for r, ln in all_import_lines:
        fi = files.get(r)
        if fi is None or fi["kind"] != "python" or tag(fi, ln) != 3:
            continue
        src_lines = _src_cache.setdefault(r, open(src + r, encoding="utf-8").read().splitlines())
        if "lambda" in src_lines[ln - 1] and fi["line_tests"].get(str(ln)):
            continue
        impall.setdefault(r, []).append(ln)

    # stmts: line spans of multi-line statements inside functions (for
    # compound statements, just the header). Coverage may credit such a
    # statement to only some of its physical lines.
    stmts: dict[str, list] = {}
    for path, fi in files.items():
        if fi["kind"] != "python" or not os.path.exists(src + path):
            continue
        try:
            tree = tree_for(path)
        except SyntaxError:
            continue
        spans = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if not isinstance(n, ast.stmt) or n is fn or isinstance(
                    n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                body = getattr(n, "body", None)
                hi = (body[0].lineno - 1) if isinstance(body, list) and body and isinstance(body[0], ast.AST) else n.end_lineno
                if hi > n.lineno:
                    spans.add((n.lineno, hi))
        if spans:
            stmts[path] = sorted(spans)

    # attrs: class-body assignments `NAME = ...` (their line spans) per
    # file; attr_reads: for each such NAME, every line in sqlglot/ or tests/
    # that reads it (`.NAME` attribute loads, or a "NAME" string constant,
    # which covers getattr/hasattr by name). A read that isn't a runtime
    # function-body line (class body, module level, import-time code) makes
    # the attribute "*": its value is consumed at class creation and may be
    # propagated anywhere, so the selector keeps its conservative handling.
    attrs: dict[str, list] = {}
    for path, fi in files.items():
        if fi["kind"] != "python" or not path.startswith("sqlglot/") or not os.path.exists(src + path):
            continue
        try:
            tree = tree_for(path)
        except SyntaxError:
            continue
        spans = []
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for st in cls.body:
                targets = []
                if isinstance(st, ast.Assign):
                    targets = [t.id for t in st.targets if isinstance(t, ast.Name)]
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                    targets = [st.target.id]
                if len(targets) == 1:
                    spans.append([st.lineno, st.end_lineno, targets[0]])
        if spans:
            attrs[path] = spans
    wanted = {name for spans in attrs.values() for _, _, name in spans}
    impall_sets = {r: set(v) for r, v in impall.items()}
    reads: dict[str, set] = {}
    star: set[str] = set()
    for path, fi in files.items():
        if fi["kind"] != "python" or not os.path.exists(src + path):
            continue
        try:
            tree = tree_for(path)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            name = None
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and n.attr in wanted:
                name = n.attr
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in wanted:
                name = n.value
            if name is None or name in star:
                continue
            ln = n.lineno
            if tag(fi, ln) != 3 or ln in impall_sets.get(path, ()):
                star.add(name)
                continue
            reads.setdefault(name, set()).add((path, ln))
    attr_reads = {}
    for name in wanted:
        # No static read at all means it's read reflectively (e.g. the
        # generator dispatches `<key>_sql` methods via getattr(self,
        # f"{key}_sql"); parsers look up `_parse_*` similarly): fall back.
        dynamic = name.endswith("_sql") or name.startswith("_parse") or not reads.get(name)
        attr_reads[name] = "*" if (name in star or dynamic) else sorted([p, l] for p, l in reads[name])
    index["attr_reads"] = attr_reads

    index["eager_files"] = eager
    for path, fi in files.items():
        fi.pop("imp", None)
        fi.pop("sig", None)
        fi.pop("strspans", None)
        fi.pop("lazy", None)
        fi.pop("impall", None)
        fi.pop("stmts", None)
        fi.pop("attrs", None)
        if path in attrs:
            fi["attrs"] = attrs[path]
        if path in impall:
            fi["impall"] = sorted(set(impall[path]))
        if path in stmts:
            fi["stmts"] = [list(x) for x in stmts[path]]
        if path in lazy and fi["kind"] == "python":
            fi["lazy"] = sorted(lazy[path])
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
