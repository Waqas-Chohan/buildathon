"""Run tests against mutants in isolated sqlglot copies (dev harness).

usage: python tools/mutrun.py <jobs.json> <results.json> [workers]
jobs: [{"id": ..., "patch": path, "tests": [nodeid, ...]}]
Results are resumable: finished ids in results.json are skipped. Each copy is
restored from the pristine clone after every job, so an interrupted run never
leaves a mutant behind.
"""
import json, os, queue, shutil, subprocess, sys, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
TOOLS = Path(__file__).resolve().parent
W = TOOLS.parent / ".work"
SRC = W / "sqlglot-src"; PY = W / "venv/bin/python"
jobs = json.load(open(sys.argv[1])); out = sys.argv[2]; nw = int(sys.argv[3]) if len(sys.argv) > 3 else 6
LOCK = threading.Lock()
results = json.load(open(out)) if os.path.exists(out) else {}
slots = queue.Queue()


def fresh(d):
    for sub in ("sqlglot", "tests"):
        if (d / sub).exists():
            shutil.rmtree(d / sub)
        shutil.copytree(SRC / sub, d / sub, ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("setup.cfg", "pyproject.toml"):
        shutil.copy(SRC / f, d / f)


for i in range(nw):
    d = W / f"run{i}"; d.mkdir(exist_ok=True); fresh(d); slots.put(d)


def run(job):
    if job["id"] in results:
        return
    d = slots.get()
    try:
        patch = str(Path(job["patch"]).resolve())
        r = subprocess.run(["patch", "-p1", "-s", "-i", patch], cwd=d, capture_output=True, text=True)
        if r.returncode:
            res = {"error": "patch failed " + r.stdout + r.stderr}
        elif not job["tests"]:
            res = {"failed": [], "passed": []}
        else:
            oc = d / "outcome.json"
            if oc.exists():
                oc.unlink()
            env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{d}:{TOOLS}", OUTCOME_OUT=str(oc),
                       PYTHONDONTWRITEBYTECODE="1")
            cmd = [str(PY), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "outcome_plugin",
                   "--timeout", "180", *job["tests"]]
            try:
                subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=900, env=env)
                res = json.load(open(oc)) if oc.exists() else {"error": "no outcome"}
            except subprocess.TimeoutExpired:
                res = {"error": "timeout"}
        with LOCK:
            results[job["id"]] = res
            json.dump(dict(results), open(out, "w"))
    finally:
        fresh(d)
        slots.put(d)


with ThreadPoolExecutor(nw) as ex:
    list(ex.map(run, jobs))
print("done", len(results))
