"""Build-time TPC-DS data for the run-time test_execute_tpcds check.

Runs in the Dockerfile against the pristine sqlglot checkout. Writes, under
<outdir>:
  tables/<name>.pkl  (columns, rows) parsed exactly like
                     TestExecutor.setUpClass does (csv + ast.literal_eval)
  queries.pkl        {"schema": TPCDS_SCHEMA, "queries": [(meta, sql), ...]}
  baseline.pkl       per query: its fingerprint at the pinned commit, None
                     for a query the test only parses, or UNSTABLE when the
                     fingerprint differs between two hash seeds (never
                     trusted at run time)

usage: python tpcds_prep.py <sqlglot-src> <outdir> [--no-baseline]
"""
from __future__ import annotations

import ast
import csv
import gzip
import os
import pickle
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UNSTABLE = "UNSTABLE"


def open_file(file_name):
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    if gzipped:
        return gzip.open(file_name, "rt", newline="")
    return open(file_name, encoding="utf-8", newline="")


def write_tables(src: str, outdir: str) -> None:
    sys.path.insert(0, src)
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    os.makedirs(os.path.join(outdir, "tables"), exist_ok=True)
    d = os.path.join(src, "tests/fixtures/optimizer/tpc-ds/")
    for table, columns in TPCDS_SCHEMA.items():
        reader = csv.reader(open_file(f"{d}{table}.csv.gz"), delimiter="|")
        rows, ctypes = [], []
        next(reader)
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        with open(os.path.join(outdir, "tables", f"{table}.pkl"), "wb") as f:
            pickle.dump((columns, rows), f, protocol=pickle.HIGHEST_PROTOCOL)
    queries = [(meta, sql) for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")]
    with open(os.path.join(outdir, "queries.pkl"), "wb") as f:
        pickle.dump({"schema": TPCDS_SCHEMA, "queries": queries}, f, protocol=pickle.HIGHEST_PROTOCOL)


def baseline_for_seed(src: str, outdir: str, seed: str) -> list:
    out = os.path.join(outdir, f"baseline.{seed}.pkl")
    code = (
        "import pickle,sys; sys.path[:0]=[sys.argv[1],sys.argv[2]];"
        "import tpcds_common as tc;"
        "q=pickle.load(open(sys.argv[3]+'/queries.pkl','rb'));"
        "raw=tc.TableStore(sys.argv[3]+'/tables');"
        "pickle.dump([tc.fingerprint(m,s,q['schema'],raw) for m,s in q['queries']],open(sys.argv[4],'wb'))"
    )
    subprocess.run([sys.executable, "-c", code, src, HERE, outdir, out], check=True,
                   env=dict(os.environ, PYTHONHASHSEED=seed))
    with open(out, "rb") as f:
        res = pickle.load(f)
    os.remove(out)
    return res


def main():
    src, outdir = sys.argv[1], sys.argv[2]
    write_tables(src, outdir)
    if "--no-baseline" in sys.argv:
        return
    a = baseline_for_seed(src, outdir, "0")
    b = baseline_for_seed(src, outdir, "1")
    base = [x if x == y else UNSTABLE for x, y in zip(a, b)]
    with open(os.path.join(outdir, "baseline.pkl"), "wb") as f:
        pickle.dump(base, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"tpcds baseline: {sum(x is not None and x != UNSTABLE for x in base)} executed, "
          f"{sum(x == UNSTABLE for x in base)} unstable, {len(base)} total", file=sys.stderr)


if __name__ == "__main__":
    main()
