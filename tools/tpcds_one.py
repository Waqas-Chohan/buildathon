"""Run the tpcds check for given commits, reporting verdict and child CPU:
python tools/tpcds_one.py <data_dir> <commit...>"""
import importlib.util, os, resource, sys, tempfile, time, shutil
from pathlib import Path
repo = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("selector", repo / "selector.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ix = m.load_index(repo / "index.json.gz"); m.configure(ix)
check = m.TpcdsCheck(repo / ".work/spec")
tid = ix["test_ids"].index(m.TPCDS_TEST_ID)
data = Path(sys.argv[1]); rc = {}
allq = "--all" in sys.argv
for c in [a for a in sys.argv[2:] if not a.startswith("--")]:
    text = (data / "commits" / f"{c}.patch").read_text()
    secs = m.parse_diff(text)
    qs = check.queries(secs, ix["files"], rc, tid)
    if allq:
        qs = list(range(check.n))
    if not qs:
        print(c, "not checkable"); continue
    wd = Path(tempfile.mkdtemp())
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    p = check.start(text, qs, wd / "x", int(2 * check.estimate(qs)) + 2)
    out, _ = p.communicate()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    shutil.rmtree(wd)
    print(c, out.decode().strip(), "queries", len(qs), "est", round(check.estimate(qs), 2), "cpu", round(cpu, 2), flush=True)
