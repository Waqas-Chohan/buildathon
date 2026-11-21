"""Blast Radius selector.

Reads /data/commits/*.patch and /data/tests.json, consults the precomputed
index (/app/index.json, built offline at Docker build time from a coverage-
instrumented baseline run of the pinned sqlglot commit), and prints one JSON
line per commit: which tests to run and how to split them across workers.

No network, no coverage/pytest needed here -- just stdlib.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
INDEX_PATH = Path("/app/index.json")
MAX_WORKERS = 8
WORKER_COST_MS = 2000

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# sqlglot splits each dialect's logic across sibling subpackages
# (sqlglot/dialects/tsql.py, sqlglot/parsers/tsql.py, sqlglot/generators/tsql.py,
# sqlglot/typing/tsql.py). A class/module-level attribute defined in one of
# these (e.g. a config flag like WEEK_OFFSET) typically executes exactly once,
# at import time -- coverage.py attributes that single execution to whichever
# test happened to import the module first, not to the (possibly very many)
# tests in OTHER sibling files whose methods actually *read* that attribute.
# Per-line/per-file coverage alone can't see across that file boundary, so
# when such an unreliable line changes, also pull in every sibling file's
# full test set for the same dialect name -- purely additive, never removes
# a candidate, which is the safe direction to err in (a miss costs far more
# than a few extra tests).
_DIALECT_FILE_RE = re.compile(r"^sqlglot/(dialects|parsers|generators|typing)/([^/]+)\.py$")
_DIALECT_DIRS = ("dialects", "parsers", "generators", "typing")

# Several dialects subclass ANOTHER dialect, not just the base Dialect class
# -- and not just the outer class either: their nested Tokenizer/Generator/
# Parser classes explicitly inherit from the parent dialect's nested classes
# too (e.g. "class Trino(Presto): class Tokenizer(Presto.Tokenizer): ..."),
# confirmed by reading every dialects/*.py file directly. A classbody-level
# change in sqlglot/dialects/presto.py (or its parsers/generators/typing
# siblings) can silently affect Trino, and transitively Athena and Dune,
# none of which share "presto" as a dialect name, so the sibling-by-name
# escalation above never reaches them. Map each dialect to its direct
# children so we can walk the whole subtree.
#
# Athena is the one dialect in the whole codebase with TWO parents, not
# one, and it's not even plain class inheritance for most of it -- verified
# by reading dialects/athena.py, parsers/athena.py and generators/athena.py
# directly:
#   - dialects/athena.py's Tokenizer builds its IDENTIFIERS/STRING_ESCAPES/
#     HEX_STRINGS/UNICODE_STRINGS/NUMERIC_LITERALS/KEYWORDS by concatenating
#     Trino.Tokenizer's and Hive.Tokenizer's class attributes, and also
#     instantiates a live Hive().tokenizer() it dispatches to at runtime.
#   - parsers/athena.py imports Hive and instantiates a real hive.parser()
#     (self._hive_parser) it delegates to for Hive-flavored statements.
#   - generators/athena.py instantiates both a Hive-subclassing
#     _HiveGenerator and a Trino-subclassing AthenaTrinoGenerator and
#     dispatches generate() to whichever matches the expression.
# So besides the already-mapped presto->trino->athena inheritance chain,
# Athena has a second, independent dependency straight on Hive that a
# change to sqlglot/{dialects,parsers,generators}/hive.py must also reach.
#
# Presto has exactly the same shape of problem, verified the same way --
# confirmed with an exhaustive grep across every dialects/parsers/
# generators/typing/*.py file for "ATTR = OtherDialect.ATTR" classbody
# assignments and "**OtherDialect.ATTR" dict-merges, then checked each hit
# against the existing inheritance map (almost all of them, e.g. every
# Spark2/Hive, Trino/Presto, Databricks/Spark, Doris-SingleStore-StarRocks/
# MySQL and Redshift-RisingWave-Materialize/Postgres merge, are just a
# dialect extending its OWN already-mapped parent's dict -- unsurprising
# and already handled):
#   - sqlglot/dialects/presto.py's Presto class body has
#     "TIME_FORMAT = MySQL.TIME_FORMAT" and "TIME_MAPPING = MySQL.
#     TIME_MAPPING" -- copied once, at class-creation time, from a dialect
#     Presto does NOT inherit from at all.
#   - sqlglot/generators/presto.py calls self.format_time(expression,
#     Hive.INVERSE_TIME_MAPPING, Hive.INVERSE_TIME_TRIE) -- Hive's own
#     derived time-mapping table (computed from Hive's TIME_MAPPING by the
#     _Dialect metaclass), read straight off the Hive class, to generate
#     Presto/Trino/Athena/Dune datetime SQL.
# So a classbody-level change to mysql.py OR hive.py must also escalate to
# presto (and transitively, via the existing presto->trino->{athena,dune}
# edge, to all of Presto's own descendants too).
_DIALECT_CHILDREN: dict[str, tuple[str, ...]] = {
    "presto": ("trino",),
    "trino": ("athena", "dune"),
    "hive": ("spark2", "athena", "presto"),
    "spark2": ("spark",),
    "spark": ("databricks",),
    "mysql": ("doris", "singlestore", "starrocks", "presto"),
    "postgres": ("materialize", "redshift", "risingwave"),
    "tsql": ("fabric",),
}


def _dialect_descendants(name: str) -> list[str]:
    out: list[str] = []
    stack = list(_DIALECT_CHILDREN.get(name, ()))
    while stack:
        child = stack.pop()
        if child not in out:
            out.append(child)
            stack.extend(_DIALECT_CHILDREN.get(child, ()))
    return out

# A handful of files define the base classes almost everything else in the
# codebase inherits from or is built on (Generator, Parser, Tokenizer,
# Dialect, Expression, and -- one level down -- Scope/Resolver in the
# optimizer and Table/Context/Plan/Schema in the executor). A class/
# module-level attribute on one of these is the same "executes once at
# import, coverage sees only the first importer" problem as the
# sibling-dialect case, except the blast radius isn't a handful of
# sibling files -- it can be most of the optimizer or executor test
# suite, since every rule module / dialect subclasses or operates on one
# of these. Found exactly this failure mode once already in production
# (a real submission run missed 2 commits we have no visibility into) --
# rather than guess which specific attribute it was, treat any
# non-body-level change to one of these foundational files as "select
# everything". A few extra seconds of test time per such commit is far
# cheaper than a 300s miss, and these files change rarely in practice.
_FOUNDATIONAL_FILES = frozenset({
    "sqlglot/generator.py",
    "sqlglot/parser.py",
    "sqlglot/tokens.py",
    "sqlglot/tokenizer_core.py",
    "sqlglot/dialects/dialect.py",
    "sqlglot/expressions/core.py",
    "sqlglot/optimizer/scope.py",
    "sqlglot/optimizer/resolver.py",
    "sqlglot/executor/table.py",
    "sqlglot/executor/context.py",
    "sqlglot/executor/env.py",
    "sqlglot/planner.py",
    "sqlglot/schema.py",
    # dialects/dialect.py's base Dialect class defaults every dialect's
    # parser_class/generator_class/tokenizer_class to Generator (generator.py,
    # already listed), Tokenizer (tokens.py, already listed), and BaseParser --
    # defined here, NOT in dialects/, so _dialect_sibling_tests (which only
    # pairs up same-named files across dialects/parsers/generators/typing)
    # never finds it: there's no dialects/base.py to pair it with. Any dialect
    # that doesn't override parser_class inherits straight from this.
    "sqlglot/parsers/base.py",
    # Same shape of gap for jsonpath_tokenizer_class = JSONPathTokenizer,
    # defined in jsonpath.py subclassing the base Tokenizer.
    "sqlglot/jsonpath.py",
    # Same shape of gap one more time, confirmed by grepping every
    # dialects/*.py file for "TypeAnnotator." (the class defined here):
    # sqlglot/dialects/bigquery.py builds its own COERCES_TO dict as
    # "**TypeAnnotator.COERCES_TO, <bigquery overrides>" at class-body
    # time, with zero inheritance relationship to this file at all --
    # bigquery.py doesn't subclass TypeAnnotator, it just copies its dict
    # once. Any dialect's type-annotation tests could be relying on this
    # same base COERCES_TO table even without copying it verbatim, so
    # treat this file the same as the other optimizer/executor base
    # classes above rather than chase down every individual reader.
    "sqlglot/optimizer/annotate_types.py",
})


def is_trivial(line_content: str) -> bool:
    s = line_content.strip()
    return s == "" or s.startswith("#")


def _strip_prefix(p: str) -> str:
    p = p.split("\t", 1)[0].strip()
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def parse_diff(patch_text: str):
    """Yields (old_path, new_path, changed_old_lines, insert_anchors) per file."""
    lines = patch_text.splitlines()
    i, n = 0, len(lines)
    results = []
    old_path = new_path = None
    changed_old: set[int] = set()
    insert_anchors: set[int] = set()
    have_section = False

    def flush():
        if have_section:
            results.append((old_path, new_path, changed_old, insert_anchors))

    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            flush()
            old_path = new_path = None
            changed_old = set()
            insert_anchors = set()
            have_section = True
            i += 1
            continue
        if line.startswith("--- "):
            p = line[4:]
            old_path = None if p.strip() == "/dev/null" else _strip_prefix(p)
            i += 1
            continue
        if line.startswith("+++ "):
            p = line[4:]
            new_path = None if p.strip() == "/dev/null" else _strip_prefix(p)
            i += 1
            continue
        m = HUNK_RE.match(line)
        if m:
            old_cursor = int(m.group(1))
            i += 1
            while i < n and not lines[i].startswith("@@") and not lines[i].startswith("diff --git "):
                hl = lines[i]
                if hl.startswith("---") or hl.startswith("+++"):
                    i += 1
                    continue
                if hl.startswith("-"):
                    if not is_trivial(hl[1:]):
                        changed_old.add(old_cursor)
                    old_cursor += 1
                elif hl.startswith("+"):
                    if not is_trivial(hl[1:]):
                        insert_anchors.add(max(old_cursor - 1, 1))
                elif hl.startswith(" ") or hl == "":
                    # A context line for a blank source line can come through
                    # with its leading marker space stripped (trailing-
                    # whitespace trimming in some diff generators), appearing
                    # as a totally empty line rather than a single space. If
                    # we don't count it, every line-number computation for
                    # the rest of this hunk silently shifts by one -- treat
                    # it as a context line, not as something to ignore.
                    old_cursor += 1
                # lines starting with "\" (no newline at EOF) or anything else: ignore
                i += 1
            continue
        i += 1
    flush()
    return results


class FileResolver:
    def __init__(self, file_index: dict):
        self.idx = file_index
        self._agg_cache: dict[tuple[int, int], set[int]] = {}

    def aggregate(self, lo: int, hi: int) -> set[int]:
        key = (lo, hi)
        cached = self._agg_cache.get(key)
        if cached is not None:
            return cached
        acc: set[int] = set()
        line_tests = self.idx["line_tests"]
        for ln in range(lo, hi + 1):
            t = line_tests.get(str(ln))
            if t:
                acc.update(t)
        self._agg_cache[key] = acc
        return acc

    def find_tag(self, line: int):
        for lo, hi, code, kidx in self.idx["tag_runs"]:
            if lo <= line <= hi:
                return code, kidx
        return 0, None

    def range_bounds_for_key(self, kidx: int):
        key = self.idx["keys"][kidx]
        if key == "module:<module>":
            return 1, self.idx["file_hi"]
        for lo, hi, k in self.idx["ranges"]:
            if k == kidx:
                return lo, hi
        return 1, self.idx["file_hi"]

    def tightest_range(self, line: int):
        best = None
        best_w = None
        for lo, hi, _kidx in self.idx["ranges"]:
            if lo <= line <= hi:
                w = hi - lo
                if best_w is None or w < best_w:
                    best, best_w = (lo, hi), w
        return best or (1, self.idx["file_hi"])

    def select_for_changed_line(self, line: int, selected: set[int]) -> bool:
        """Returns True iff this was a precise (body-level) lookup -- i.e. no
        escalation beyond this file was needed."""
        code, kidx = self.find_tag(line)
        if code == 3 and kidx is not None:  # body: precise per-line lookup
            t = self.idx["line_tests"].get(str(line))
            if t:
                selected.update(t)
            return True
        if kidx is None:
            selected.update(self.aggregate(1, self.idx["file_hi"]))
            return False
        lo, hi = self.range_bounds_for_key(kidx)
        selected.update(self.aggregate(lo, hi))
        return False

    def select_for_insertion(self, anchor: int, selected: set[int]) -> None:
        lo, hi = self.tightest_range(anchor)
        selected.update(self.aggregate(lo, hi))


def _dialect_sibling_tests(dialect_name: str, files_index: dict, resolver_cache: dict,
                            selected: set[int]) -> None:
    for name in (dialect_name, *_dialect_descendants(dialect_name)):
        for d in _DIALECT_DIRS:
            sib_path = f"sqlglot/{d}/{name}.py"
            sib_index = files_index.get(sib_path)
            if sib_index is None or sib_index["kind"] != "python":
                continue
            resolver = resolver_cache.get(sib_path)
            if resolver is None:
                resolver = FileResolver(sib_index)
                resolver_cache[sib_path] = resolver
            selected.update(resolver.aggregate(1, sib_index["file_hi"]))


def resolve_commit(sections, files_index: dict, resolver_cache: dict) -> tuple[set[int], bool]:
    """Returns (selected_test_ints, select_all). select_all is True when a
    non-precise change touched a foundational base-class file, in which case
    the caller should run the full suite for this commit rather than trust
    the (necessarily incomplete) selected set."""
    selected: set[int] = set()
    select_all = False
    for old_path, new_path, changed_old, insert_anchors in sections:
        path = old_path if old_path is not None else new_path
        if path is None:
            continue
        file_index = files_index.get(path)
        if file_index is None:
            continue
        if file_index["kind"] == "fixture":
            if changed_old or insert_anchors:
                selected.update(file_index["tests"])
            continue

        resolver = resolver_cache.get(path)
        if resolver is None:
            resolver = FileResolver(file_index)
            resolver_cache[path] = resolver

        needs_dialect_escalation = False
        for line in changed_old:
            precise = resolver.select_for_changed_line(line, selected)
            if not precise:
                needs_dialect_escalation = True
                if path in _FOUNDATIONAL_FILES:
                    select_all = True
        if insert_anchors:
            needs_dialect_escalation = True
            if path in _FOUNDATIONAL_FILES:
                select_all = True
        for anchor in insert_anchors:
            resolver.select_for_insertion(anchor, selected)

        if needs_dialect_escalation:
            m = _DIALECT_FILE_RE.match(path)
            if m:
                _dialect_sibling_tests(m.group(2), files_index, resolver_cache, selected)
    return selected, select_all


def pack_workers(tests_with_durations: list[tuple[int, int]]) -> list[list[int]]:
    if not tests_with_durations:
        return []
    n = len(tests_with_durations)
    best_workers: list[list[int]] | None = None
    best_cost: int | None = None
    sorted_tests = sorted(tests_with_durations, key=lambda t: t[1], reverse=True)
    for k in range(1, min(MAX_WORKERS, n) + 1):
        workers: list[list[int]] = [[] for _ in range(k)]
        loads = [0] * k
        for num, dur in sorted_tests:
            idx = loads.index(min(loads))
            workers[idx].append(num)
            loads[idx] += dur
        cost = max(loads) + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    assert best_workers is not None
    return best_workers


def main() -> None:
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    test_ids = index["test_ids"]
    files_index = index["files"]

    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    id_to_number = {t["id"]: t["number"] for t in tests}
    duration_ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_numbers = [t["number"] for t in tests]

    resolver_cache: dict[str, FileResolver] = {}
    patches = sorted((DATA / "commits").glob("*.patch"))
    done = 0
    fallback_count = 0

    for patch_path in patches:
        commit = patch_path.stem
        try:
            text = patch_path.read_text(encoding="utf-8", errors="replace")
            sections = parse_diff(text)
            selected_ints, select_all = resolve_commit(sections, files_index, resolver_cache)
            if select_all:
                selected_numbers = set(all_numbers)
            else:
                selected_numbers = set()
                for i in selected_ints:
                    tid = test_ids[i]
                    num = id_to_number.get(tid)
                    if num is not None:
                        selected_numbers.add(num)
        except Exception as exc:  # noqa: BLE001 - never let one bad patch sink the batch
            print(f"selector: fallback for {commit}: {exc!r}", file=sys.stderr)
            selected_numbers = set(all_numbers)
            fallback_count += 1

        tests_with_durations = [(num, duration_ms.get(num, 0)) for num in selected_numbers]
        workers = pack_workers(tests_with_durations)
        print(json.dumps({"commit": commit, "workers": workers}))
        done += 1

    print(f"selector: {done} commits, {fallback_count} fallbacks", file=sys.stderr)


if __name__ == "__main__":
    main()
