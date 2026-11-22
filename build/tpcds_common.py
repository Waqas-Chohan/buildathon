"""Replays test_execute_tpcds one query at a time (build time and run time).

tests/test_executor.py::TestExecutor::test_execute_tpcds, per query:
  main process   parse_one(sql), pickled to a spawned worker
  worker         find_tables, execute(expression, schema, tables), result
                 pickled back
  main process   transpile(sql, write="duckdb") -> DuckDB result, compared
                 with assert_frame_equal against the worker's rows
fingerprint() performs the same sqlglot calls in-process and returns what the
assertion depends on. Equal fingerprints mean the assertion sees identical
inputs, so a query whose fingerprint under a patch equals the pinned commit's
fingerprint passes exactly as it did there.

Run time (as a script, by selector.py, in a fresh interpreter whose sys.path
starts with a patched copy of sqlglot):
  python tpcds_common.py <patched_root> <spec_dir> <i,j,k...>
prints "PASS" if every listed query reproduces its baseline, else "FAIL".
"""
from __future__ import annotations

import os
import pickle
import sys

UNSTABLE = "UNSTABLE"


class TableStore:
    """{table: (columns, rows)}, loaded per table on first use."""

    def __init__(self, directory: str):
        self.dir = directory
        self.cache: dict = {}

    def __getitem__(self, name):
        v = self.cache.get(name)
        if v is None:
            with open(os.path.join(self.dir, f"{name}.pkl"), "rb") as f:
                v = self.cache[name] = pickle.load(f)
        return v


def fingerprint(meta: dict, sql: str, schema: dict, raw, tables_for=None):
    """tables_for(name) -> Table; by default a fresh pickled copy per query
    (build time). The run-time replay passes the worker's shared tables."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table

    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        return None
    tables = {}
    for t in find_tables(expression):  # same order as mp_execute's dict
        if tables_for is None:
            columns, rows = raw[t.name]
            tables[t.name] = Table(columns=columns, rows=rows)
        else:
            tables[t.name] = tables_for(t.name)
    if tables_for is None:
        tables = pickle.loads(pickle.dumps(tables))
    table = execute(expression, schema=schema, tables=tables)
    table = pickle.loads(pickle.dumps(table))
    duck = transpile(sql, write="duckdb")[0]
    return (repr(tuple(table.columns)), repr(list(table.rows)), duck)


def replay(qs, q: dict, base: list, raw) -> bool:
    """Replays queries `qs` the way one pool worker would: one unpickled
    schema and one set of unpickled tables shared by every query. Passes
    only if every fingerprint matches the baseline AND the shared schema and
    tables are unchanged afterwards (a later query in the same worker would
    read them)."""
    from sqlglot.executor.table import Table

    schema = pickle.loads(pickle.dumps(q["schema"]))
    shared: dict = {}

    def tables_for(name):
        t = shared.get(name)
        if t is None:
            columns, rows = raw[name]
            t = shared[name] = pickle.loads(pickle.dumps(Table(columns=columns, rows=rows)))
        return t

    for i in qs:
        if base[i] == UNSTABLE:
            return False
        meta, sql = q["queries"][i]
        if fingerprint(meta, sql, schema, raw, tables_for) != base[i]:
            return False
    if schema != q["schema"]:
        return False
    for name, t in shared.items():
        columns, rows = raw[name]
        if tuple(t.columns) != tuple(columns) or list(t.rows) != rows:
            return False
    return True


def main() -> None:
    root, spec, which = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, root)
    with open(os.path.join(spec, "queries.pkl"), "rb") as f:
        q = pickle.load(f)
    with open(os.path.join(spec, "baseline.pkl"), "rb") as f:
        base = pickle.load(f)
    raw = TableStore(os.path.join(spec, "tables"))
    try:
        ok = replay([int(x) for x in which.split(",")], q, base, raw)
    except BaseException:  # noqa: BLE001 - any error means the test would fail
        ok = False
    sys.stdout.write("PASS\n" if ok else "FAIL\n")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
