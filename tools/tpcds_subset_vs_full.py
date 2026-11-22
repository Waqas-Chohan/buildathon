"""For each (set, commit): replay the coverage-chosen query subset and all 99
queries; a subset PASS with a full FAIL means the coverage map missed a query.
usage: python tools/tpcds_subset_vs_full.py <list.txt: set commit n> <out.json> [jobs]"""
import importlib.util, json, os, shutil, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
repo = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("selector", repo / "selector.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ix = m.load_index(repo / "index.json.gz"); m.configure(ix)
check = m.TpcdsCheck(repo / ".work/spec")
tid = ix["test_ids"].index(m.TPCDS_TEST_ID)
items = [l.split() for l in open(sys.argv[1]) if l.strip()]
out = sys.argv[2]; jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 6
res = json.load(open(out)) if os.path.exists(out) else {}
rc = {}
plans = []
for s, c, _ in items:
    key = f"{s}/{c}"
    if key in res: continue
    text = (repo / ".work" / s / "commits" / f"{c}.patch").read_text()
    qs = check.queries(m.parse_diff(text), ix["files"], rc, tid)
    plans.append((key, text, qs))
import threading
lock = threading.Lock()
def run(key, text, qs, limit):
    wd = Path(tempfile.mkdtemp())
    try:
        p = check.start(text, qs, wd / "x", limit)
        if p is None: return "NOAPPLY"
        o, _ = p.communicate()
        return o.decode().strip() or "KILLED"
    finally:
        shutil.rmtree(wd)
def one(plan):
    key, text, qs = plan
    sub = run(key, text, qs, 120)
    full = run(key, text, list(range(check.n)), 200) if sub == "PASS" else None
    with lock:
        res[key] = {"n": len(qs), "subset": sub, "full": full}
        json.dump(res, open(out, "w"))
    if sub == "PASS" and full != "PASS":
        print("DISAGREE", key, len(qs), full, flush=True)
with ThreadPoolExecutor(jobs) as ex:
    list(ex.map(one, plans))
from collections import Counter
print(Counter((v["subset"], v["full"]) for v in res.values()))
