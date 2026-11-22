"""Run selector.py against a data dir: python tools/runsel.py <data_dir> [selector.py] > out.jsonl"""
import importlib.util, sys, time
from pathlib import Path
repo = Path(__file__).resolve().parent.parent
data = Path(sys.argv[1]); sel = Path(sys.argv[2]) if len(sys.argv) > 2 else repo / "selector.py"
spec = importlib.util.spec_from_file_location("selector", sel)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.DATA = data; m.INDEX_PATH = repo / "index.json.gz"
import os
for k, v in (("SPEC_DIR", repo / (os.environ.get("SPEC") or ".work/spec")), ("PACKED_INDEX_PATH", repo / ".work/index.pack")):
    if hasattr(m, k):
        setattr(m, k, v)
import resource
t = time.process_time(); m.main()
ch = resource.getrusage(resource.RUSAGE_CHILDREN)
print("cpu", round(time.process_time() - t, 3), "children", round(ch.ru_utime + ch.ru_stime, 3), file=sys.stderr)
