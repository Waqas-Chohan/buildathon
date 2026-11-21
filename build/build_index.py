#!/usr/bin/env python3
"""Builds the Blast Radius test-selection index.

Run once, at Docker BUILD time (uncharged), against a full instrumented test
run of sqlglot at the pinned commit. Produces a single compact JSON file
consumed by selector.py at RUN time (no coverage/pytest needed there).

Inputs (produced by the build script that drives pytest):
  - .coverage              sqlite db from `pytest --cov --cov-context=test`
  - fixture_touches.json   {relpath: [test_id, ...]} from conftest.py's open() tracker

Output shape (index.json):
{
  "test_ids": ["tests/...::test_a", ...],      # global int -> test id
  "files": {
    "<relpath>": {
      "kind": "python",
      "tag_runs": [[lo, hi, tag_code, key_idx], ...],   # sorted, non-overlapping, covers 1..file_hi
      "keys": ["function:foo", "class:Bar", ...],        # key_idx indexes into this, local to file
      "ranges": [[lo, hi, key_idx], ...],                 # function/class spans only, for anchor search
      "line_tests": {"<lineno>": [test_int, ...]},        # only lines with coverage
      "file_hi": N
    } | {
      "kind": "fixture",
      "tests": [test_int, ...]
    }
  }
}

tag_code: 0=module 1=classbody 2=header 3=body
"""
from __future__ import annotations

import ast
import json
import os
import sys

from coverage import CoverageData

TAG_CODES = {"module": 0, "classbody": 1, "header": 2, "body": 3}


def strip_context_phase(context: str) -> str | None:
    if not context:
        return None
    return context.rsplit("|", 1)[0]


def load_raw_line_tests(coverage_data_path: str, repo_root: str) -> dict[str, dict[int, set[str]]]:
    data = CoverageData(basename=coverage_data_path)
    data.read()
    result: dict[str, dict[int, set[str]]] = {}
    repo_root = os.path.abspath(repo_root)
    for abs_path in data.measured_files():
        abs_path_n = os.path.abspath(abs_path)
        if not abs_path_n.startswith(repo_root + os.sep):
            continue
        rel = os.path.relpath(abs_path_n, repo_root).replace(os.sep, "/")
        ctx_by_line = data.contexts_by_lineno(abs_path)
        line_tests: dict[int, set[str]] = {}
        for lineno, contexts in ctx_by_line.items():
            tests = {strip_context_phase(c) for c in contexts} - {None}
            if tests:
                line_tests[lineno] = tests
        if line_tests:
            result[rel] = line_tests
    return result


class FileScopeInfo:
    __slots__ = ("line_tags", "ranges", "file_hi")

    def __init__(self):
        self.line_tags: dict[int, tuple[str, str]] = {}
        self.ranges: list[tuple[int, int, str]] = []
        self.file_hi = 1


def _has_docstring(body: list[ast.stmt]) -> ast.stmt | None:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0]
    return None


def analyze_file_scopes(source: str) -> FileScopeInfo | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    info = FileScopeInfo()
    info.file_hi = max(1, len(source.splitlines()))

    def tag_range(lo: int, hi: int, tag: str, key: str) -> None:
        for ln in range(lo, hi + 1):
            info.line_tags[ln] = (tag, key)

    def visit_body(stmts: list[ast.stmt], kind: str, key: str | None) -> None:
        for stmt in stmts:
            visit_stmt(stmt, kind, key)

    def visit_stmt(stmt: ast.stmt, kind: str, key: str | None) -> None:
        lo, hi = stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno)

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = f"{key}.{stmt.name}" if key else stmt.name
            func_key = f"function:{qual}"
            tag_range(lo, hi, "body", func_key)
            info.ranges.append((lo, hi, func_key))

            header_lo = stmt.decorator_list[0].lineno if stmt.decorator_list else stmt.lineno
            body_stmts = stmt.body
            doc = _has_docstring(body_stmts)
            if doc:
                first_real = body_stmts[1] if len(body_stmts) > 1 else None
            else:
                first_real = body_stmts[0] if body_stmts else None
            header_hi = (first_real.lineno - 1) if first_real and first_real.lineno > stmt.lineno else stmt.lineno
            header_hi = max(header_hi, stmt.lineno)
            tag_range(header_lo, header_hi, "header", func_key)
            if doc:
                tag_range(doc.lineno, getattr(doc, "end_lineno", doc.lineno), "header", func_key)
                body_stmts = body_stmts[1:]
            visit_body(body_stmts, "function", qual)

        elif isinstance(stmt, ast.ClassDef):
            qual = f"{key}.{stmt.name}" if key else stmt.name
            class_key = f"class:{qual}"
            tag_range(lo, hi, "classbody", class_key)
            info.ranges.append((lo, hi, class_key))

            header_lo = stmt.decorator_list[0].lineno if stmt.decorator_list else stmt.lineno
            first_body = stmt.body[0] if stmt.body else None
            header_hi = (first_body.lineno - 1) if first_body and first_body.lineno > stmt.lineno else stmt.lineno
            header_hi = max(header_hi, stmt.lineno)
            tag_range(header_lo, header_hi, "header", class_key)
            visit_body(stmt.body, "class", qual)

        else:
            default_tag = "body" if kind == "function" else ("classbody" if kind == "class" else "module")
            default_key = f"{kind}:{key}" if key else "module:<module>"
            tag_range(lo, hi, default_tag, default_key)
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    visit_stmt(child, kind, key)

    visit_body(tree.body, "module", None)
    return info


def run_length_encode_tags(line_tags: dict[int, tuple[str, str]], file_hi: int,
                            key_index: dict[str, int]) -> list[list[int]]:
    runs: list[list[int]] = []
    prev = None
    run_lo = 1
    for ln in range(1, file_hi + 1):
        tag, key = line_tags.get(ln, ("module", "module:<module>"))
        code = TAG_CODES[tag]
        kidx = key_index.setdefault(key, len(key_index))
        cur = (code, kidx)
        if prev is None:
            run_lo = ln
            prev = cur
        elif cur != prev:
            runs.append([run_lo, ln - 1, prev[0], prev[1]])
            run_lo = ln
            prev = cur
    if prev is not None:
        runs.append([run_lo, file_hi, prev[0], prev[1]])
    return runs


def build_index(repo_root: str, coverage_data_path: str, fixture_touches_path: str) -> dict:
    raw = load_raw_line_tests(coverage_data_path, repo_root)
    fixture_touches = {}
    if os.path.exists(fixture_touches_path):
        fixture_touches = json.load(open(fixture_touches_path, encoding="utf-8"))

    test_id_to_int: dict[str, int] = {}

    def intern(tid: str) -> int:
        i = test_id_to_int.get(tid)
        if i is None:
            i = len(test_id_to_int)
            test_id_to_int[tid] = i
        return i

    all_py_relpaths: set[str] = set(raw.keys())
    for root, _dirs, filenames in os.walk(repo_root):
        if os.sep + ".git" in root:
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, fn), repo_root).replace(os.sep, "/")
                all_py_relpaths.add(rel)

    files_out: dict[str, dict] = {}

    for rel in sorted(all_py_relpaths):
        abspath = os.path.join(repo_root, rel)
        try:
            with open(abspath, encoding="utf-8") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        scope = analyze_file_scopes(source)
        if scope is None:
            continue
        line_tests_raw = raw.get(rel, {})

        key_index: dict[str, int] = {}
        tag_runs = run_length_encode_tags(scope.line_tags, scope.file_hi, key_index)
        # ranges need the same key indices
        ranges_out = []
        for lo, hi, key in scope.ranges:
            kidx = key_index.setdefault(key, len(key_index))
            ranges_out.append([lo, hi, kidx])

        line_tests_out = {
            str(ln): sorted(intern(t) for t in tests)
            for ln, tests in line_tests_raw.items()
        }

        keys_list = [None] * len(key_index)
        for key, idx in key_index.items():
            keys_list[idx] = key

        files_out[rel] = {
            "kind": "python",
            "tag_runs": tag_runs,
            "keys": keys_list,
            "ranges": ranges_out,
            "line_tests": line_tests_out,
            "file_hi": scope.file_hi,
        }

    for rel, tests in fixture_touches.items():
        files_out[rel] = {"kind": "fixture", "tests": sorted(intern(t) for t in tests)}

    test_ids_list = [None] * len(test_id_to_int)
    for tid, idx in test_id_to_int.items():
        test_ids_list[idx] = tid

    return {"test_ids": test_ids_list, "files": files_out}


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: build_index.py <repo_root> <coverage_data_file> <fixture_touches.json> <out_index.json>",
            file=sys.stderr,
        )
        return 2
    repo_root, coverage_path, fixture_path, out_path = sys.argv[1:5]
    index = build_index(repo_root, coverage_path, fixture_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    num_py = sum(1 for v in index["files"].values() if v["kind"] == "python")
    num_fx = sum(1 for v in index["files"].values() if v["kind"] == "fixture")
    print(
        f"indexed {num_py} python files, {num_fx} fixture files, "
        f"{len(index['test_ids'])} tests -> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
