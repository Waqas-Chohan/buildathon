# How index.json is built

Everything here runs once, offline, against sqlglot at the pinned commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, with the packages from
`requirements.lock` and `SKIP_INTEGRATION=0` (the default in
`tests/helpers.py`). The selector only reads the resulting `index.json`.

## 1. Per-line coverage and fixture map (`build_index.py`)

```sh
cd sqlglot-src
cp /path/to/build/conftest.py /path/to/build/coveragerc .   # fixture-open tracker
pytest --cov=sqlglot --cov-context=test --cov-config=coveragerc tests
python /path/to/build/build_index.py .coverage fixture_touches.json > index.json
```

This gives each source line the tests that executed it, a module/class/
header/body tag per line, function/class ranges, and each fixture file's
readers. `conftest.py` also credits fixtures read in a unittest
`setUpClass` to every test in that class.

## 2. Import-time code, signatures, eager modules, string spans

```sh
python build/augment_import_time.py sqlglot-src index.json
```

Some function bodies only run while a module or class is being created.
Examples are `_Dialect.__new__`, `Tokenizer.__init_subclass__`,
`_build_coerces_to`, and builder helpers called inside class-body dicts.
Coverage gives those lines to nobody, or only to whichever test imported
the dialect first. This step traces a full `import sqlglot` and the
import of every submodule, and writes these fields:

* `imp`: for each such function, its span, the import-executed lines, and the files whose
  module/class body triggered it (the owners of the state it built).
* `eager_files`: modules a bare `import sqlglot` loads. Their import-time
  state is global.
* `sig`: parameter lines of multi-line `def` signatures. Defaults are
  evaluated at def time.
* `strspans`: multi-line string literals whose value is used at runtime.
  A comment or blank line added inside one is a real edit.

## 3. Expression-class users

```sh
cd sqlglot-src
EXPRMAP_DIR=/tmp/exprmap PYTHONPATH=/path/to/build:. \
    pytest -p exprclass_plugin -n 2 tests
python /path/to/build/augment_exprclass.py . /tmp/exprmap /path/to/index.json
```

Expression classes are mostly declarations (`arg_types`,
`is_var_len_args`, ...), and those lines never run at test time. The
plugin records which expression classes each test instantiates.
`exprcls` stores, for each class, the tests that use it or any subclass
that inherits its declarations.

## Validation

A mutation harness (applied mutants: `and`/`or`, `True`/`False`, `==`/`!=`,
`is`/`is not`, dropping `not`, comparison flips, integer +1) checked the
selector against the real suite. It ran every unselected test against
each mutant, so any failure there is a proven miss. The old selector
missed in every category fixed by steps 2 and 3. Examples:
`simplify.py:1984` (import-time dispatch table) and `temporal.py:54`
(`arg_types` flip).

## 4. Ship it

```sh
gzip -9 -n -c index.json > index.json.gz   # what the Dockerfile copies
```

The image ships the gzip-compressed index. It is about 0.9 MB instead of
about 13 MB, which keeps the repository well under the 20 MB limit.
`selector.py` decompresses it with the standard library.

## 5. test_execute_tpcds replay (`tpcds_*.py`)

`test_execute_tpcds` (21.5 s) runs 99 TPC-DS queries, 57 of them through
sqlglot's Python executor, and compares the results with DuckDB. A runtime
edit in the optimizer or executor usually reaches only a few of those
queries, so `selector.py` can replay exactly those queries against the
patched code at run time and drop the test when every one reproduces the
pinned commit's result.

* `tpcds_cov.json.gz` (committed, like the index): per-query *cold*
  coverage. Each query ran in a fresh interpreter under coverage, started
  before `import sqlglot`, so it is credited with import-time code and lazily
  built caches as well as its own execution path. Also holds every file's
  executable lines and each query's CPU cost. Regenerate with:

  ```sh
  python build/tpcds_prep.py sqlglot-src spec --no-baseline
  python build/tpcds_coverage.py sqlglot-src spec build/tpcds_cov.json.gz /tmp/parts 6
  ```

* `tpcds_prep.py` (runs in the Dockerfile): parses the TPC-DS tables exactly
  as `TestExecutor.setUpClass` does and records each query's fingerprint
  (result columns and rows, and the DuckDB transpilation) at the pinned
  commit under two hash seeds. A query whose fingerprint depends on the seed
  is marked unstable and never trusted.
* `tpcds_common.py` (shipped): the replay. Like one pool worker, it shares a
  single unpickled schema and set of tables across the queries it runs, and
  fails if either was modified afterwards.

The selector only attempts the replay when every edit is a 1:1 replacement
of runtime function-body lines (tight mode's rules) at unchanged
indentation, and when its estimated CPU cost is under half the simulated
seconds dropping the test saves. A replay that fails, raises, or exceeds its
CPU limit keeps the test.

## 6. Packed index (`pack_index.py`)

The Dockerfile repacks `index.json.gz` into `/app/index.pack`: a JSON header
plus one JSON slice per file, decoded only when a commit touches that file.
Loading drops from ~0.5 s to under 0.01 s of CPU per run.
