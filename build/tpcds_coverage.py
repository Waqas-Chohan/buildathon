"""Per-query cold coverage for test_execute_tpcds (offline, like index.json).

Each query runs in a fresh interpreter under coverage, started before
`import sqlglot`, so a query is credited with every sqlglot line its result
can depend on: module and class bodies, lazily built caches, Table
construction and pickling, parsing, execution and the DuckDB transpilation.
Index -1 is the setup alone (import + building and pickling every table, as
setUpClass and the pool initializer do) and is credited to every query.

Output (gzip JSON):
  {"n": 99,
   "cost": [CPU seconds per query on the build machine, untraced],
   "files": {relpath: {"lines": {line: query_bitmask}, "exe": [executable lines]}}}

usage: python tpcds_coverage.py <sqlglot-src> <spec_dir> <out.json.gz> <parts_dir> [jobs]
(spec_dir from tpcds_prep.py)
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))

CHILD = r"""
import json, os, pickle, sys, time
src, spec, idx, out, traced = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5] == "1"
sys.path[:0] = [src, %r]
if traced:
    import coverage
    cov = coverage.Coverage(data_file=None, source=[os.path.join(src, "sqlglot")], branch=False)
    cov.start()
import tpcds_common as tc
q = pickle.load(open(os.path.join(spec, "queries.pkl"), "rb"))
raw = tc.TableStore(os.path.join(spec, "tables"))
from sqlglot.executor.table import Table
pickle.loads(pickle.dumps({n: Table(columns=raw[n][0], rows=raw[n][1]) for n in q["schema"]}))
t0 = time.process_time()
if idx >= 0:
    meta, sql = q["queries"][idx]
    tc.fingerprint(meta, sql, q["schema"], raw)
dt = time.process_time() - t0
res = {"cpu": dt}
if traced:
    cov.stop()
    data = cov.get_data()
    files = {}
    for f in data.measured_files():
        rel = os.path.relpath(f, src).replace(os.sep, "/")
        exe = sorted(cov.analysis2(f)[1])
        files[rel] = {"lines": sorted(data.lines(f) or ()), "exe": exe}
    res["files"] = files
json.dump(res, open(out, "w"))
""" % HERE


def main():
    src, spec, out, tmp = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 4
    import pickle
    with open(os.path.join(spec, "queries.pkl"), "rb") as f:
        n = len(pickle.load(f)["queries"])
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, PYTHONHASHSEED="0")

    def one(arg):
        i, traced = arg
        p = os.path.join(tmp, f"{i}.{traced}.json")
        if not os.path.exists(p):
            subprocess.run([sys.executable, "-c", CHILD, src, spec, str(i), p, str(traced)], check=True, env=env)
        with open(p) as f:
            return arg, json.load(f)

    work = [(i, 1) for i in range(-1, n)] + [(i, 0) for i in range(n)]
    with ThreadPoolExecutor(jobs) as ex:
        parts = dict(ex.map(one, work))
    raw_cost = [parts[(i, 0)]["cpu"] for i in range(n)]
    files: dict[str, dict] = {}
    allmask = (1 << n) - 1
    for i in range(-1, n):
        bit = allmask if i < 0 else (1 << i)
        for f, info in parts[(i, 1)]["files"].items():
            d = files.setdefault(f, {"lines": {}, "exe": set()})
            d["exe"].update(info["exe"])
            for ln in info["lines"]:
                d["lines"][ln] = d["lines"].get(ln, 0) | bit
    doc = {
        "n": n,
        "cost": [round(c, 4) for c in raw_cost],
        "files": {
            f: {"lines": {str(k): v for k, v in sorted(d["lines"].items())}, "exe": sorted(d["exe"])}
            for f, d in sorted(files.items())
        },
    }
    with open(out, "wb") as f:
        f.write(gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), 9, mtime=0))
    print(f"queries {n} files {len(files)} cpu {sum(raw_cost):.2f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
