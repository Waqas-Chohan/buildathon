"""Blast Radius selector.

Reads /data/commits/*.patch and /data/tests.json, consults the precomputed
index (/app/index.json, built offline at Docker build time from a coverage-
instrumented baseline run of the pinned sqlglot commit), and prints one JSON
line per commit: which tests to run and how to split them across workers.

No network, no coverage/pytest needed here -- just stdlib.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DATA = Path("/data")
# Shipped gzip-compressed: ~0.9 MB instead of ~13 MB keeps the repository
# well inside the 20 MB limit, and decompressing is cheaper than the extra
# I/O (measured: 0.23 s vs 0.25 s CPU to load).
INDEX_PATH = Path("/app/index.json.gz")

# Run-time test_execute_tpcds check (see tpcds_check below). Everything
# under SPEC_DIR is built by the Dockerfile from the pinned commit; when it
# is missing the check is simply skipped and the test stays selected.
SPEC_DIR = Path("/app/spec")


# The Dockerfile repacks index.json.gz into this layout (build/pack_index.py)
# so each file's entry is decoded only when a commit needs it.
PACKED_INDEX_PATH = Path("/app/index.pack")


class LazyFiles:
    """Read-only {path: entry} over a packed index; decodes on first get."""

    def __init__(self, body: bytes, offsets: dict):
        self._body = body
        self._offsets = offsets
        self._cache: dict = {}

    def get(self, path, default=None):
        v = self._cache.get(path)
        if v is None:
            off = self._offsets.get(path)
            if off is None:
                return default
            v = self._cache[path] = json.loads(bytes(self._body[off[0]:off[0] + off[1]]))
        return v


def load_index(path: Path) -> dict:
    if path.suffix == ".pack":
        raw = path.read_bytes()
        nl = raw.index(b"\n")
        index = json.loads(raw[:nl])
        index["files"] = LazyFiles(memoryview(raw)[nl + 1:], index.pop("offsets"))
        return index
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return json.loads(raw)
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
    # NOTE: sqlglot/optimizer/annotate_types.py was added here on the
    # theory that bigquery.py's "**TypeAnnotator.COERCES_TO" copy needed
    # the same full-suite safety net as the files above. A real submission
    # proved that wrong: cost went up (~12s) with the same 1 commit still
    # missed, meaning this fired on at least one real commit for no
    # benefit. Checked why: annotate_types.py's class-level "ranges" entry
    # for the whole TypeAnnotator class spans its entire body (line 160 to
    # EOF, one single range covering every method), so the ALREADY-EXISTING
    # generic classbody aggregate (the non-foundational fallback a few
    # lines below) aggregates over that whole range for ANY classbody-level
    # change in this file -- which already pulls in 21 bigquery tests
    # alongside everything else that calls into TypeAnnotator's methods.
    # Verified directly: aggregate(160, 1192) on the current index already
    # includes tests/dialects/test_bigquery.py tests. The foundational
    # (select-everything) treatment was solving an already-solved problem
    # at full-suite cost. Removed.
})


def is_trivial(line_content: str) -> bool:
    s = line_content.strip()
    return s == "" or s.startswith("#")


# PEP 263: a comment on line 1 or 2 matching this sets the file's encoding.
_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=]")


def _trivial_at(line_content: str, line: int) -> bool:
    return is_trivial(line_content) and not (line <= 2 and _CODING_RE.match(line_content))


def _strip_prefix(p: str) -> str:
    p = p.split("\t", 1)[0].strip()
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def _code_tokens(line: str):
    """Token strings of one source line (None if it doesn't tokenize alone)."""
    import io
    import tokenize

    try:
        toks = tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline)
        return [t.string for t in toks if t.type not in (
            tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER, tokenize.INDENT,
            tokenize.DEDENT, tokenize.COMMENT)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def _none_identity_to_equality(tokens):
    out, i = [], 0
    while i < len(tokens):
        if tokens[i] == "is" and i + 2 < len(tokens) and tokens[i + 1] == "not" and tokens[i + 2] == "None":
            out += ["!=", "None"]
            i += 3
        elif tokens[i] == "is" and i + 1 < len(tokens) and tokens[i + 1] == "None":
            out += ["==", "None"]
            i += 2
        else:
            out.append(tokens[i])
            i += 1
    return out


def is_none_comparison_rewrite(old: str, new: str) -> bool:
    """`x is None` <-> `x == None` (and `is not` <-> `!=`) and nothing else.

    Equivalent for every object sqlglot compares with None: builtins,
    Expression.__eq__ (False unless type(other) is type(self)), Dialect and
    _Dialect.__eq__ (type comparisons). Token-level, so text inside string
    literals never matches. Not used for `is True/False` (1 == True) or
    `is <object>` (== is structural equality on expressions)."""
    a, b = _code_tokens(old), _code_tokens(new)
    if a is None or b is None or a == b:
        return False
    return _none_identity_to_equality(a) == _none_identity_to_equality(b)


def parse_diff(patch_text: str):
    """Returns one tuple per file section:
        (old_path, new_path, changed_old, insert_anchors,
         trivial_old, trivial_anchors, binary)
    changed_old / insert_anchors hold non-comment, non-blank edits (in old-
    file line numbers); trivial_old / trivial_anchors hold comment/blank
    edits, which are only harmless for Python outside string literals --
    resolve_commit decides. binary is True for a binary patch (no hunks)."""
    lines = patch_text.splitlines()
    i, n = 0, len(lines)
    results = []
    sec: dict | None = None

    def flush():
        if sec is not None:
            equiv = sec["pairs_ok"] and sec["npairs"] > 0 and not sec["bin"]
            results.append((sec["old"], sec["new"], sec["chg"], sec["ins"],
                            sec["tchg"], sec["tins"], sec["bin"], equiv))

    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            flush()
            sec = {"old": None, "new": None, "chg": set(), "ins": set(),
                   "tchg": set(), "tins": set(), "bin": False,
                   "pairs_ok": True, "npairs": 0}
            # Paths from the header too, so a section with no ---/+++ lines
            # (binary patch, pure mode change) still names its file.
            m = _DIFF_GIT_RE.match(line)
            if m:
                sec["old"], sec["new"] = m.group(1), m.group(2)
            i += 1
            continue
        if sec is None:
            i += 1
            continue
        if line.startswith("--- "):
            p = line[4:]
            sec["old"] = None if p.strip() == "/dev/null" else _strip_prefix(p)
            i += 1
            continue
        if line.startswith("+++ "):
            p = line[4:]
            sec["new"] = None if p.strip() == "/dev/null" else _strip_prefix(p)
            i += 1
            continue
        if line.startswith("new file mode"):
            sec["old"] = None
        elif line.startswith("deleted file mode"):
            sec["new"] = None
        elif line.startswith("GIT binary patch") or (
            line.startswith("Binary files ") and line.rstrip().endswith(" differ")
        ):
            sec["bin"] = True
        m = HUNK_RE.match(line)
        if m:
            old_cursor = int(m.group(1))
            # Use the hunk's own line counts to know where it ends, rather
            # than guessing from line prefixes: a removed SQL comment
            # "-- x" shows up as "--- x" and an added one as "+-- x", which
            # a prefix check would mistake for file headers.
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            minus: list[str] = []
            plus: list[str] = []

            def close_block():
                # Pair a run of non-trivial "-" lines with the "+" lines that
                # replace it, to recognise provably equivalent rewrites.
                if minus or plus:
                    if len(minus) == len(plus) and all(
                        is_none_comparison_rewrite(a, b) for a, b in zip(minus, plus)
                    ):
                        sec["npairs"] += len(minus)
                    else:
                        sec["pairs_ok"] = False
                minus.clear()
                plus.clear()

            i += 1
            while i < n and (old_left > 0 or new_left > 0):
                hl = lines[i]
                if hl.startswith("\\"):  # "\ No newline at end of file"
                    i += 1
                    continue
                if hl.startswith("diff --git ") or HUNK_RE.match(hl):
                    break  # truncated hunk: stop rather than misread
                if hl.startswith("-"):
                    if _trivial_at(hl[1:], old_cursor):
                        sec["tchg"].add(old_cursor)
                    else:
                        sec["chg"].add(old_cursor)
                        if plus:
                            close_block()
                        minus.append(hl[1:])
                    old_cursor += 1
                    old_left -= 1
                elif hl.startswith("+"):
                    if _trivial_at(hl[1:], old_cursor):
                        sec["tins"].add(max(old_cursor - 1, 1))
                    else:
                        sec["ins"].add(max(old_cursor - 1, 1))
                        plus.append(hl[1:])
                    new_left -= 1
                else:
                    close_block()
                    # Context line. One for a blank source line can come
                    # through with its leading space stripped (trailing-
                    # whitespace trimming), i.e. as a totally empty line --
                    # still a context line, or every later line number in
                    # the hunk shifts by one.
                    old_cursor += 1
                    old_left -= 1
                    new_left -= 1
                i += 1
            close_block()
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

    def insertion_is_precise(self, anchor: int) -> bool:
        """An edit landing after old line `anchor` (a replaced line's "+"
        side has anchor == that line) stays inside one runtime function
        body: both neighbours are body lines of the same function and
        neither runs at import time."""
        c1, k1 = self.find_tag(anchor)
        c2, k2 = self.find_tag(anchor + 1)
        return (
            c1 == 3 and c2 == 3 and k1 == k2 and k1 is not None
            and self.import_time_entry(anchor) is None
            and self.import_time_entry(anchor + 1) is None
            and not self.is_lazy(anchor) and not self.is_lazy(anchor + 1)
        )

    # -- tight mode: exact selection for genuinely runtime edits --
    #
    # Per-test coverage of a function-body line is exact when the line only
    # runs at call time: a test whose outcome changes must execute it. The
    # exceptions are all measured at build time and excluded here: lines
    # executed during import at ANY depth ("impall", e.g. parser code run
    # while a dialect parses SQL into class constants), lazy process-wide
    # caches ("lazy"), def-time signature defaults ("sig"), and import-time
    # builders ("imp"). Coverage can also credit a wrapped statement to only
    # some of its physical lines, so a line inside a multi-line statement
    # takes the tests of the whole statement ("stmts").
    def _runtime_line(self, line: int) -> bool:
        code, kidx = self.find_tag(line)
        if code != 3 or kidx is None:
            return False
        sets = getattr(self, "_tight_sets", None)
        if sets is None:
            sets = self._tight_sets = (
                frozenset(self.idx.get("impall", ())),
                frozenset(self.idx.get("lazy", ())),
            )
        impall, lazy = sets
        return (
            line not in impall
            and line not in lazy
            and self.import_time_entry(line) is None
            and self.signature_span(line) is None
        )

    def _line_tests_exact(self, line: int, selected: set[int]) -> None:
        t = self.idx["line_tests"].get(str(line))
        if t:
            selected.update(t)
        for lo, hi in self.idx.get("stmts", ()):
            if lo <= line <= hi:
                selected.update(self.aggregate(lo, hi))

    def take_runtime_edits(self, changed_old, insert_anchors, selected):
        """Select exact tests for runtime edits; return the edits that still
        need the conservative treatment."""
        rest_changed = set()
        for line in changed_old:
            if self._runtime_line(line):
                self._line_tests_exact(line, selected)
            else:
                rest_changed.add(line)
        rest_anchors = set()
        for a in insert_anchors:
            if a in changed_old:
                # "+" side of a replaced line: it runs wherever the old line
                # ran, so the removed line's handling covers it -- unless
                # that line itself needs the conservative path.
                if a in rest_changed:
                    rest_anchors.add(a)
                continue
            if (
                self._runtime_line(a)
                and self._runtime_line(a + 1)
                and self.find_tag(a)[1] == self.find_tag(a + 1)[1]
            ):
                self._line_tests_exact(a, selected)
                self._line_tests_exact(a + 1, selected)
            else:
                rest_anchors.add(a)
        return rest_changed, rest_anchors

    def is_lazy(self, line: int) -> bool:
        """First-use initialisation whose result is cached process-wide
        (measured at build time, e.g. generator.py's _build_dispatch filling
        _DISPATCH_CACHE): credited only to the first test that triggered it."""
        lazy = getattr(self, "_lazy", None)
        if lazy is None:
            lazy = self._lazy = frozenset(self.idx.get("lazy", ()))
        return line in lazy

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

    # -- import-time / def-time lines (see build/augment_import_time.py) --
    #
    # Some function-body lines only ever run while a module or class is
    # being created: metaclass/__init_subclass__ hooks (_Dialect.__new__,
    # Tokenizer.__init_subclass__), table builders (_build_coerces_to,
    # _build_gen_dispatch), trie.new_trie, builder helpers called inside
    # class-body dicts. Their per-test attribution is empty or just "whoever
    # imported this dialect first" (~12 tests for _Dialect.__new__), yet the
    # state they build is read by everything after. Treating them as precise
    # selected those few tests AND skipped every escalation, including the
    # foundational select-all -- a guaranteed miss for any mutation there.
    def import_time_entry(self, line: int):
        """Returns (func_lo, func_hi, owner_files) if `line` is a body line that
        runs at import time, else None."""
        lookup = getattr(self, "_imp_lookup", None)
        if lookup is None:
            lookup = {}
            for lo, hi, owners, lines in self.idx.get("imp", ()):
                for ln in lines:
                    lookup.setdefault(ln, (lo, hi, owners))
            self._imp_lookup = lookup
        return lookup.get(line)

    def import_time_span(self, anchor: int):
        """For an insertion: the innermost import-time function whose span
        contains `anchor` (code inserted there would also run at import)."""
        best = None
        for lo, hi, owners, _lines in self.idx.get("imp", ()):
            if lo < anchor <= hi and (best is None or hi - lo < best[1] - best[0]):
                best = (lo, hi, owners)
        return best

    # -- class-level declarations (see build/exprclass_plugin.py) --
    #
    # Class bodies are mostly declarations -- arg_types / is_var_len_args on
    # expression classes, feature flags such as SET_OP_LIMITS or TIME_MAPPING
    # on Dialect/Tokenizer/Parser/Generator subclasses -- whose lines never
    # run at test time; base-class code reads them. Coverage attributes them
    # to nobody (12.8% of all mutable source lines had an EMPTY selection),
    # and for dialects it misses tests that only *write* that dialect: a
    # T-SQL test generating Teradata SQL runs only base Generator code with
    # TeradataGenerator's flags (mutation-tested: SET_OP_LIMITS True->False
    # broke test_tsql's test_set_operation_branch_limits, outside every
    # coverage-based selection). The tests that depend on a class's
    # declarations are the ones that instantiate it or a subclass inheriting
    # them; that set was recorded at build time and is looked up here.
    def class_user_tests(self, line: int):
        """Tests depending on the declarations of the innermost tracked class
        containing `line`: None (not in one), "*" (nearly the whole suite),
        or a list of test indices."""
        users = self.idx.get("clsusers")
        if not users:
            return None
        keys = self.idx["keys"]
        best = None
        for lo, hi, kidx in self.idx["ranges"]:
            if lo <= line <= hi and keys[kidx].startswith("class:"):
                if best is None or hi - lo < best[1] - best[0]:
                    best = (lo, hi, keys[kidx][6:])
        if best is None:
            return None
        return users.get(best[2])

    def string_line_removed(self, line: int) -> bool:
        """Removing old line `line` changes a runtime multi-line string
        literal spanning [lo, hi] (opening quote on lo, closing on hi)."""
        return any(lo < line < hi for lo, hi in self.idx.get("strspans", ()))

    def string_insert_after(self, anchor: int) -> bool:
        """Inserting a line after old line `anchor` lands inside one."""
        return any(lo <= anchor < hi for lo, hi in self.idx.get("strspans", ()))

    def signature_span(self, line: int):
        """(func_lo, func_hi) if `line` is a parameter line of a multi-line
        def signature (defaults are evaluated at def time, not per call)."""
        for p_lo, p_hi, f_lo, f_hi in self.idx.get("sig", ()):
            if p_lo <= line <= p_hi:
                return f_lo, f_hi
        return None


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


_EAGER_FILES: frozenset = frozenset()

# When True, a foundational file only escalates to the full suite for
# class/module-level, header, import-time or ambiguous edits; an edit
# inside an ordinary method body uses that method's exact test set like any
# other file. DO NOT ENABLE: a foundational-only mutation campaign found a
# miss within 9 mutants (parser.py:4653 `if not self._match(TokenType.INTO)`
# -> `if self._match(...)` broke Athena/BigQuery tests outside the precise
# selection). The full-suite fallback for core files is load-bearing.
_PRECISE_FOUNDATIONAL = False

# Tight mode (see FileResolver.take_runtime_edits). Off until mutation
# testing shows it never misses.
_TIGHT = True

# Skip files whose only edits are provably equivalent rewrites.
_EQUIV = True

# Class-body data attributes select only tests that read them (see
# _take_attr_edits). Off until mutation testing shows it never misses.
_ATTR = True


_ATTR_READS: dict = {}


def configure(index: dict) -> None:
    """Load index-wide settings (call once before resolve_commit)."""
    global _EAGER_FILES, _ATTR_READS
    _EAGER_FILES = frozenset(index.get("eager_files", ()))
    _ATTR_READS = index.get("attr_reads", {})
    _TESTS_BY_FILE.clear()
    for i, tid in enumerate(index.get("test_ids", ())):
        _TESTS_BY_FILE.setdefault(tid.split("::", 1)[0], []).append(i)


def _take_attr_edits(resolver, changed_old, insert_anchors, selected, files_index, resolver_cache):
    """Class-body data attributes (`SET_OP_LIMITS = True`, `WEEK_OFFSET = -1`)
    only matter where they are read. When every read of that name anywhere
    in sqlglot/tests is an ordinary runtime line (attr_reads[name] is a
    list -- build time marks it "*" if any read happens at class creation /
    import time, or if it's never read statically, i.e. read reflectively),
    the affected tests are those executing a read, restricted to tests that
    use this class or a subclass. Returns the edits left for the
    conservative path."""
    reads_map = _ATTR_READS
    spans = resolver.idx.get("attrs")
    if not spans or not reads_map:
        return changed_old, insert_anchors

    def attr_at(line):
        for lo, hi, name in spans:
            if lo <= line <= hi:
                return name
        return None

    handled = set()
    for line in changed_old:
        if resolver.find_tag(line)[0] != 1:
            continue
        name = attr_at(line)
        reads = reads_map.get(name) if name else None
        if not isinstance(reads, list):
            continue
        hit: set[int] = set()
        for path, ln in reads:
            fi = files_index.get(path)
            if fi is not None:
                hit.update(fi["line_tests"].get(str(ln), ()))
        users = resolver.class_user_tests(line)
        if isinstance(users, list):
            hit &= set(users)
        selected.update(hit)
        handled.add(line)
    if not handled:
        return changed_old, insert_anchors
    rest_changed = set(changed_old) - handled
    rest_anchors = {a for a in insert_anchors if a not in handled}
    return rest_changed, rest_anchors


# Tests that enumerate sqlglot's own files (pkgutil over sqlglot.typing,
# sqlglotc/setup.py's os.listdir of subpackages), so a brand-new module can
# change what they see.
_PACKAGE_WALKING_TESTS = ("tests/test_typing.py", "tests/test_sqlglotc.py")
_TESTS_BY_FILE: dict[str, list[int]] = {}


def _tests_in_files(paths) -> list[int]:
    out: list[int] = []
    for p in paths:
        out.extend(_TESTS_BY_FILE.get(p, ()))
    return out


# pytest reads its configuration from these; the pinned commit has none, so
# introducing one (or a pytest section in pyproject.toml / setup.cfg) can
# change how every test runs.
_PYTEST_CONFIG_FILES = frozenset({"pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini"})
_PYTEST_SECTION_FILES = frozenset({"pyproject.toml", "setup.cfg"})


def touches_pytest_config(patch_text: str) -> bool:
    for chunk in patch_text.split("diff --git ")[1:]:
        m = _DIFF_GIT_RE.match("diff --git " + chunk.split("\n", 1)[0])
        if not m:
            continue
        names = {m.group(1), m.group(2)}
        if names & _PYTEST_CONFIG_FILES:
            return True
        if names & _PYTEST_SECTION_FILES and "pytest" in chunk:
            return True
    return False


def resolve_commit(sections, files_index: dict, resolver_cache: dict) -> tuple[set[int], bool]:
    """Returns (selected_test_ints, select_all). select_all is True when a
    non-precise change touched a foundational base-class file, in which case
    the caller should run the full suite for this commit rather than trust
    the (necessarily incomplete) selected set."""
    selected: set[int] = set()
    select_all = False
    for (old_path, new_path, changed_old, insert_anchors,
         trivial_old, trivial_anchors, binary, equivalent) in sections:
        path = old_path if old_path is not None else new_path
        if path is None:
            continue
        name = path.rsplit("/", 1)[-1]
        if name == "conftest.py" or (new_path or "").rsplit("/", 1)[-1] == "conftest.py":
            # The pinned commit has no conftest.py anywhere; one appearing or
            # changing can hook every test. (index.json's own conftest.py
            # entry is the build-time tracker, not part of sqlglot.)
            select_all = True
            continue
        # Deleted or renamed away: everything that imported, called or read
        # the old path breaks, exactly as if every line had been removed.
        gone = old_path is not None and new_path != old_path
        file_index = files_index.get(path)
        if file_index is None:
            if old_path is None and new_path is not None:
                if new_path == "sqlglot/_version.py":
                    # sqlglot/__init__.py imports it when present.
                    select_all = True
                elif new_path.startswith("sqlglot/") and new_path.endswith(".py"):
                    # A new module is imported by nothing at the pinned
                    # commit, but some tests enumerate the package's files.
                    selected.update(_tests_in_files(_PACKAGE_WALKING_TESTS))
            continue
        if file_index["kind"] == "fixture":
            # In sqlglot's .sql fixtures a "#" line is not a comment: it is
            # metadata (# title:, # execute:, # dialect: ...) that
            # tests/helpers.py's _extract_meta/load_sql_fixtures attach to
            # the next statement. Any edit to a fixture, including a binary
            # one (.csv.gz), a deletion or a rename, counts.
            if changed_old or insert_anchors or trivial_old or trivial_anchors or binary or gone:
                selected.update(file_index["tests"])
            continue
        if gone:
            # Every module that imports it (transitively) now fails at import
            # time, and coverage cannot tell who those are: module bodies run
            # once, credited to whichever test imported first. Rare and
            # drastic -- run everything.
            select_all = True
            continue
        if binary:
            changed_old = set(range(1, file_index["file_hi"] + 1))
            insert_anchors = set()
            trivial_old, trivial_anchors = set(), set()
            equivalent = False
            # A test module that disappears takes its test ids with it.
            selected.update(_tests_in_files((path,)))

        resolver = resolver_cache.get(path)
        if resolver is None:
            resolver = FileResolver(file_index)
            resolver_cache[path] = resolver

        # A "# comment" or blank line is only harmless outside string
        # literals. Inside a multi-line string whose value is used at
        # runtime it changes that value, so treat it as a real edit.
        file_changed_before_strings = changed_old
        file_inserts_before_strings = insert_anchors
        if trivial_old or trivial_anchors:
            changed_old = set(changed_old)
            insert_anchors = set(insert_anchors)
            for ln in trivial_old:
                if resolver.string_line_removed(ln):
                    changed_old.add(ln)
            for a in trivial_anchors:
                if resolver.string_insert_after(a):
                    insert_anchors.add(a)

        # Every real edit in this file is an `is None` <-> `== None` style
        # rewrite (provably equivalent; see is_none_comparison_rewrite) and
        # no comment/blank edit landed inside a runtime string: nothing can
        # fail. executor/env.py is excluded: its reverse-sort-key wrapper's
        # __eq__ dereferences other.obj and would raise on None.
        if (
            _EQUIV
            and equivalent
            and path.endswith(".py")
            and path != "sqlglot/executor/env.py"
            and len(changed_old) == len(file_changed_before_strings)
            and len(insert_anchors) == len(file_inserts_before_strings)
            and not any(resolver.string_line_removed(ln) for ln in changed_old)
        ):
            continue

        if _TIGHT:
            changed_old, insert_anchors = resolver.take_runtime_edits(
                changed_old, insert_anchors, selected
            )
        if _ATTR:
            changed_old, insert_anchors = _take_attr_edits(
                resolver, changed_old, insert_anchors, selected, files_index, resolver_cache
            )


        needs_dialect_escalation = False
        import_owners: set[str] = set()
        for line in changed_old:
            precise = resolver.select_for_changed_line(line, selected)
            if resolver.is_lazy(line):
                precise = False
            imp = resolver.import_time_entry(line)
            if imp is not None:
                lo, hi, owners = imp
                selected.update(resolver.aggregate(lo, hi))
                import_owners.update(owners)
                precise = False
            sig = resolver.signature_span(line)
            if sig is not None:
                selected.update(resolver.aggregate(*sig))
            if not precise:
                users = resolver.class_user_tests(line)
                if users == "*":
                    select_all = True
                elif users:
                    selected.update(users)
            if not precise:
                needs_dialect_escalation = True
                if path in _FOUNDATIONAL_FILES:
                    select_all = True
        if insert_anchors:
            needs_dialect_escalation = True
            if path in _FOUNDATIONAL_FILES:
                if not _PRECISE_FOUNDATIONAL or any(
                    not resolver.insertion_is_precise(a) for a in insert_anchors
                ):
                    select_all = True
        for anchor in insert_anchors:
            resolver.select_for_insertion(anchor, selected)
            if resolver.find_tag(anchor)[0] != 3:
                users = resolver.class_user_tests(anchor)
                if users == "*":
                    select_all = True
                elif users:
                    selected.update(users)
            imp = resolver.import_time_span(anchor)
            if imp is not None:
                lo, hi, owners = imp
                selected.update(resolver.aggregate(lo, hi))
                import_owners.update(owners)

        if needs_dialect_escalation:
            m = _DIALECT_FILE_RE.match(path)
            if m:
                _dialect_sibling_tests(m.group(2), files_index, resolver_cache, selected)

        # State built at import time lives in (and is read through) the
        # module/class body that triggered the call -- escalate to every
        # such owner file the same way a classbody change there would be.
        for owner in import_owners:
            # An owner loaded by a bare `import sqlglot` is loaded by every
            # test, so state it builds at import time is global.
            if owner in _FOUNDATIONAL_FILES or owner in _EAGER_FILES:
                select_all = True
                continue
            owner_index = files_index.get(owner)
            if owner_index is None or owner_index["kind"] != "python":
                continue
            owner_resolver = resolver_cache.get(owner)
            if owner_resolver is None:
                owner_resolver = FileResolver(owner_index)
                resolver_cache[owner] = owner_resolver
            selected.update(owner_resolver.aggregate(1, owner_index["file_hi"]))
            om = _DIALECT_FILE_RE.match(owner)
            if om:
                _dialect_sibling_tests(om.group(2), files_index, resolver_cache, selected)
    return selected, select_all


def pack_workers(tests_with_durations: list[tuple[int, int]]) -> list[list[int]]:
    """Longest-processing-time packing for every worker count 1..8, keeping
    the cheapest (busiest load + 2 s per worker). A heap of (load, worker)
    picks the same worker as `loads.index(min(loads))` would (lowest load,
    then lowest index), in O(n log k)."""
    import heapq

    if not tests_with_durations:
        return []
    n = len(tests_with_durations)
    best_workers: list[list[int]] | None = None
    best_cost: int | None = None
    sorted_tests = sorted(tests_with_durations, key=lambda t: t[1], reverse=True)
    total = sum(d for _, d in sorted_tests)
    longest = sorted_tests[0][1]
    for k in range(1, min(MAX_WORKERS, n) + 1):
        # No packing on k workers beats this bound; skip hopeless k.
        if best_cost is not None and max(longest, -(-total // k)) + WORKER_COST_MS * k >= best_cost:
            continue
        workers: list[list[int]] = [[] for _ in range(k)]
        heap = [(0, i) for i in range(k)]
        for num, dur in sorted_tests:
            load, idx = heap[0]
            workers[idx].append(num)
            heapq.heapreplace(heap, (load + dur, idx))
        cost = max(load for load, _ in heap) + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    assert best_workers is not None
    return best_workers


# -- run-time test_execute_tpcds check ---------------------------------------
#
# test_execute_tpcds (21.5 s, the costliest test that code edits select)
# runs 99 TPC-DS queries; the 57 marked `execute` go through sqlglot's
# Python executor and are compared with DuckDB. A runtime edit in the
# optimizer or executor typically reaches only some of them. For such an
# edit we apply the patch to a private copy of the pinned sqlglot and replay
# exactly the reached queries (build/tpcds_common.py); if every one
# reproduces the pinned commit's result bit for bit, the test passes as it
# did there and is dropped. Anything else -- a different result, an
# exception, a timeout, an edit we cannot fully account for -- keeps it.
#
# Which queries an edit reaches comes from per-query *cold* coverage
# (build/tpcds_coverage.py): each query ran in a fresh interpreter under
# coverage, so it is credited with import-time code and lazily built caches
# too. A query that never executes an edited line runs identically under
# the patch. The check is only attempted when every edit is a runtime
# function-body line by the same rules tight mode uses.
TPCDS_TEST_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
# Per-query CPU costs in the map were measured on the build machine. The
# scoring machine may be slower; estimates are scaled by this factor.
TPCDS_CPU_FACTOR = 1.5
# Build-machine CPU seconds of interpreter start, patched import and table
# loading per check, on top of the queries' own cost.
TPCDS_OVERHEAD = 0.45
# Attempt the check only when its estimated CPU cost is at most this
# fraction of the simulated seconds dropping the test would save. A failing
# edit usually fails on its first query, so most of the estimate is only
# spent when the check passes.
TPCDS_MAX_COST_RATIO = 0.5
# Never spend more than this many estimated CPU seconds on checks in one
# run (keeps the 3-minute batch limit far away; deterministic, since it is
# based on build-time estimates, not on clocks).
TPCDS_TOTAL_BUDGET = 100.0


def _patch_hunks(patch_text: str):
    """{path: [(old_start, [(op, text), ...]), ...]} for plain modifications;
    None when the patch has anything else (new/deleted/renamed/binary)."""
    out: dict[str, list] = {}
    lines = patch_text.split("\n")
    i, n = 0, len(lines)
    path = None
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(line)
            if not m or m.group(1) != m.group(2):
                return None
            path = m.group(1)
            out.setdefault(path, [])
            i += 1
            continue
        if line.startswith(("new file mode", "deleted file mode", "rename ", "copy ",
                            "GIT binary patch", "Binary files ", "old mode", "new mode")):
            return None
        if line.startswith("--- ") or line.startswith("+++ "):
            if path is None or line[4:].strip() == "/dev/null":
                return None
            i += 1
            continue
        m = HUNK_RE.match(line)
        if m:
            if path is None:
                return None
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            while i < n and (old_left > 0 or new_left > 0):
                hl = lines[i]
                if hl.startswith("\\"):
                    i += 1
                    continue
                if hl.startswith("-"):
                    body.append(("-", hl[1:])); old_left -= 1
                elif hl.startswith("+"):
                    body.append(("+", hl[1:])); new_left -= 1
                else:
                    body.append((" ", hl[1:])); old_left -= 1; new_left -= 1
                i += 1
            if old_left or new_left:
                return None
            out[path].append((int(m.group(1)), body))
            continue
        i += 1
    return out


def _apply_hunks(text: str, hunks) -> str | None:
    """Applies hunks at their exact old line numbers; None on any mismatch."""
    old = text.split("\n")
    res: list[str] = []
    pos = 0  # next unconsumed index into old
    for start, body in hunks:
        idx = start - 1 if any(op != "+" for op, _ in body) else start
        if idx < pos or idx > len(old):
            return None
        res.extend(old[pos:idx])
        pos = idx
        for op, s in body:
            if op == "+":
                res.append(s)
                continue
            if pos >= len(old) or old[pos].rstrip("\r") != s.rstrip("\r"):
                return None
            if op == " ":
                res.append(old[pos])
            pos += 1
    res.extend(old[pos:])
    return "\n".join(res)


def balanced_replacements(patch_text: str) -> bool:
    """Every block of changed code lines replaces old lines one for one, at
    the same indentation (comment/blank lines aside). Then each new line runs
    exactly where the line it replaces ran; a dedented or extra line could
    run where nothing ran before."""
    hunks = _patch_hunks(patch_text)
    if not hunks:
        return False

    def indent(x: str) -> str:
        return x[: len(x) - len(x.lstrip())]

    for path, hs in hunks.items():
        for start, body in hs:
            minus: list[str] = []
            plus: list[str] = []
            for op, text in body + [(" ", "")]:
                if op == " ":
                    if len(minus) != len(plus):
                        return False
                    if any(indent(a) != indent(b) for a, b in zip(minus, plus)):
                        return False
                    minus.clear()
                    plus.clear()
                elif not is_trivial(text):
                    (minus if op == "-" else plus).append(text)
    return True


class TpcdsCheck:
    def __init__(self, spec: Path):
        with gzip.open(spec / "tpcds_cov.json.gz", "rt") as f:
            doc = json.load(f)
        self.spec = spec
        self.n = doc["n"]
        self.cost = doc["cost"]
        self.files = doc["files"]
        self._exe: dict[str, frozenset] = {}

    def queries(self, sections, files_index, resolver_cache, tid: int):
        """Indices of the TPC-DS queries the patch can affect, or None when
        it cannot be fully accounted for."""
        mask = 0
        for (old_path, new_path, changed_old, insert_anchors,
             trivial_old, trivial_anchors, binary, _equiv) in sections:
            if binary or old_path is None or old_path != new_path:
                return None
            if not (old_path.startswith("sqlglot/") and old_path.endswith(".py")):
                return None
            fi = files_index.get(old_path)
            if fi is None or fi["kind"] != "python":
                return None
            r = resolver_cache.get(old_path)
            if r is None:
                r = resolver_cache[old_path] = FileResolver(fi)
            changed = set(changed_old)
            changed.update(ln for ln in trivial_old if r.string_line_removed(ln))
            anchors = set(insert_anchors)
            anchors.update(a for a in trivial_anchors if r.string_insert_after(a))
            # Only replacements: a "+" line must take the place of a removed
            # one. Inserted code can run where neither neighbour did.
            if anchors - changed:
                return None
            if not changed:
                continue
            fc = self.files.get(old_path)
            if fc is None:
                return None
            exe = self._exe.get(old_path)
            if exe is None:
                exe = self._exe[old_path] = frozenset(fc["exe"])
            cov = fc["lines"]
            stmts = fi.get("stmts", ())
            line_tests = fi["line_tests"]
            for line in changed:
                if not r._runtime_line(line):
                    return None
                span = {line}
                for lo, hi in stmts:
                    if lo <= line <= hi:
                        span.update(range(lo, hi + 1))
                if line not in exe and len(span) == 1:
                    return None  # coverage cannot see this line at all
                m = 0
                for x in span:
                    m |= cov.get(str(x), 0)
                in_index = any(tid in line_tests.get(str(x), ()) for x in span)
                if in_index and not m:
                    return None  # the two maps disagree: trust neither
                mask |= m
        if not mask:
            return None
        return [i for i in range(self.n) if mask >> i & 1]

    def estimate(self, qs) -> float:
        """Estimated CPU seconds on the scoring machine."""
        return TPCDS_CPU_FACTOR * (TPCDS_OVERHEAD + sum(self.cost[i] for i in qs))

    def start(self, patch_text: str, qs, workdir: Path, cpu_limit: int):
        """Builds the patched tree and starts the replay; returns a Popen or
        None if the patch does not apply cleanly."""
        hunks = _patch_hunks(patch_text)
        if not hunks:
            return None
        src = self.spec / "src"
        patched = {}
        for path, hs in hunks.items():
            p = src / path
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                return None
            new = _apply_hunks(text, hs)
            if new is None:
                return None
            patched[path] = new
        root = workdir
        # Mirror the package with symlinks (pristine files keep their valid
        # precompiled bytecode), then write the patched files for real.
        for dirpath, dirnames, filenames in os.walk(src / "sqlglot"):
            rel = Path(dirpath).relative_to(src)
            (root / rel).mkdir(parents=True, exist_ok=True)
            if "__pycache__" in dirnames:
                dirnames.remove("__pycache__")
                os.symlink(Path(dirpath) / "__pycache__", root / rel / "__pycache__")
            for fn in filenames:
                relf = (rel / fn).as_posix()
                if relf in patched:
                    (root / rel / fn).write_text(patched[relf], encoding="utf-8")
                else:
                    os.symlink(Path(dirpath) / fn, root / rel / fn)
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")

        def limit():
            # A runaway edit (endless loop, blow-up) is killed by the kernel
            # after cpu_limit CPU seconds: no PASS, so the test stays.
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 1))

        return subprocess.Popen(
            [sys.executable, str(self.spec / "tpcds_common.py"), str(root), str(self.spec),
             ",".join(map(str, qs))],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env,
            preexec_fn=limit,
        )


def _pack_cost(tests_with_durations) -> int:
    workers = pack_workers(tests_with_durations)
    if not workers:
        return 0
    d = dict(tests_with_durations)
    return max(sum(d[x] for x in w) for w in workers) + WORKER_COST_MS * len(workers)


def run_tpcds_checks(check: TpcdsCheck, jobs) -> set[str]:
    """jobs: [(commit, patch_text, queries, est)]. Returns the commits whose
    replay passed. Two at a time (the run has 2 cores)."""
    import shutil
    import tempfile

    passed: set[str] = set()
    pending = list(jobs)
    running: list = []
    base = Path(tempfile.mkdtemp(prefix="tpcds-", dir="/tmp" if Path("/tmp").is_dir() else None))

    def finish(item):
        commit, proc, deadline_s, wd = item
        try:
            out, _ = proc.communicate(timeout=deadline_s)
            if proc.returncode == 0 and out.strip() == b"PASS":
                passed.add(commit)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        shutil.rmtree(wd, ignore_errors=True)

    try:
        while pending or running:
            while pending and len(running) < 2:
                commit, text, qs, est = pending.pop(0)
                wd = base / commit
                wd.mkdir()
                cpu_limit = int(1.25 * est) + 1
                try:
                    proc = check.start(text, qs, wd, cpu_limit)
                except Exception as exc:  # noqa: BLE001
                    print(f"selector: tpcds check skipped for {commit}: {exc!r}", file=sys.stderr)
                    proc = None
                if proc is None:
                    shutil.rmtree(wd, ignore_errors=True)
                    continue
                # Wall-clock backstop only; the CPU limit normally ends a
                # runaway long before this.
                running.append((commit, proc, 30.0 + 4.0 * cpu_limit, wd))
            finish(running.pop(0))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return passed


def main() -> None:
    index = load_index(PACKED_INDEX_PATH if PACKED_INDEX_PATH.is_file() else INDEX_PATH)
    configure(index)
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

    tpcds_num = id_to_number.get(TPCDS_TEST_ID)
    tpcds_int = test_ids.index(TPCDS_TEST_ID) if TPCDS_TEST_ID in test_ids else None
    check = None
    check_possible = (tpcds_num is not None and tpcds_int is not None
                      and (SPEC_DIR / "baseline.pkl").is_file())
    candidates = []  # (saving_ms / est, commit, text, queries, est)
    held: dict[str, set[int]] = {}

    def emit(commit: str, selected_numbers) -> None:
        tests_with_durations = [(num, duration_ms.get(num, 0)) for num in selected_numbers]
        workers = pack_workers(tests_with_durations)
        sys.stdout.write(json.dumps({"commit": commit, "workers": workers}) + "\n")

    for patch_path in patches:
        commit = patch_path.stem
        text = ""
        sections = None
        select_all = True
        try:
            text = patch_path.read_text(encoding="utf-8", errors="replace")
            sections = parse_diff(text)
            selected_ints, select_all = resolve_commit(sections, files_index, resolver_cache)
            if touches_pytest_config(text):
                select_all = True
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
            select_all = True
            fallback_count += 1

        if check_possible and not select_all and tpcds_num in selected_numbers:
            if check is None:
                try:
                    check = TpcdsCheck(SPEC_DIR)
                except Exception as exc:  # noqa: BLE001
                    print(f"selector: tpcds check unavailable: {exc!r}", file=sys.stderr)
                    check_possible = False
        if check is not None and not select_all and tpcds_num in selected_numbers:
            try:
                qs = check.queries(sections, files_index, resolver_cache, tpcds_int)
            except Exception as exc:  # noqa: BLE001
                print(f"selector: tpcds analysis failed for {commit}: {exc!r}", file=sys.stderr)
                qs = None
            if qs and balanced_replacements(text):
                est = check.estimate(qs)
                twd = [(num, duration_ms.get(num, 0)) for num in selected_numbers]
                saving = _pack_cost(twd) - _pack_cost([x for x in twd if x[0] != tpcds_num])
                if est * 1000 <= TPCDS_MAX_COST_RATIO * saving:
                    candidates.append((saving / est, commit, text, qs, est))
                    held[commit] = selected_numbers
                    continue
        emit(commit, selected_numbers)
        done += 1

    if candidates:
        candidates.sort(key=lambda c: (-c[0], c[1]))
        jobs, spent = [], 0.0
        for _, commit, text, qs, est in candidates:
            if spent + est <= TPCDS_TOTAL_BUDGET:
                jobs.append((commit, text, qs, est))
                spent += est
        try:
            passed = run_tpcds_checks(check, jobs)
        except Exception as exc:  # noqa: BLE001
            print(f"selector: tpcds checks failed: {exc!r}", file=sys.stderr)
            passed = set()
        print(f"selector: tpcds check passed {len(passed)} of {len(jobs)} "
              f"(est {spent:.1f} s, {len(candidates)} candidates)", file=sys.stderr)
        for commit in sorted(held):
            sel = held[commit]
            if commit in passed:
                sel = sel - {tpcds_num}
            emit(commit, sel)
            done += 1

    sys.stdout.flush()
    print(f"selector: {done} commits, {fallback_count} fallbacks", file=sys.stderr)


if __name__ == "__main__":
    main()
