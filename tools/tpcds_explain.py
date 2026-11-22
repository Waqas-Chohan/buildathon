"""Explain the tpcds check decision per commit: python tools/tpcds_explain.py <data_dir> [ids...]"""
import importlib.util, json, sys
from pathlib import Path
repo = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("selector", repo / "selector.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ix = m.load_index(repo / "index.json.gz"); m.configure(ix)
check = m.TpcdsCheck(repo / ".work/spec")
tid = ix["test_ids"].index(m.TPCDS_TEST_ID)
data = Path(sys.argv[1]); ids = sys.argv[2:]
rc = {}
for p in sorted((data / "commits").glob("*.patch")):
    if ids and p.stem not in ids:
        continue
    secs = m.parse_diff(p.read_text())
    sel, sa = m.resolve_commit(secs, ix["files"], rc)
    if tid not in sel or sa:
        continue
    qs = check.queries(secs, ix["files"], rc, tid)
    print(p.stem, "queries", None if qs is None else len(qs), "est", None if not qs else round(check.estimate(qs), 2))
