# Dev harness (not shipped)

Local tools for measuring and validating the selector. Nothing here goes into
the image (`.dockerignore` excludes `tools/`). Bulky state lives in `.work/`
(git- and docker-ignored).

Setup, once:

```sh
mkdir -p .work && cd .work
git clone https://github.com/tobymao/sqlglot.git sqlglot-src
git -C sqlglot-src checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
python3 -m venv venv && ./venv/bin/pip install -r ../requirements.lock coverage
cd .. && .work/venv/bin/python build/tpcds_prep.py .work/sqlglot-src .work/spec
mkdir -p .work/spec/src && cp -a .work/sqlglot-src/sqlglot .work/spec/src/
.work/venv/bin/python -m compileall -q .work/spec/src/sqlglot
ln -s $PWD/build/tpcds_common.py $PWD/build/tpcds_cov.json.gz .work/spec/
python3 build/pack_index.py index.json.gz .work/index.pack
```

| Tool | Purpose |
| --- | --- |
| `mutgen.py <src> <out> <n> <seed>` | Single-line mutants in the style of the sample commits (`MUT_DIRS=sqlglot/optimizer/,...` to focus). |
| `runsel.py <data_dir> [selector.py]` | Run a selector on a data dir (commits/ + tests.json); `SPEC=nonexistent` disables the tpcds replay. |
| `costs.py <tests.json> <out.jsonl> [answers.json\|meta.json]` | Simulated cost (and misses, given answers). |
| `mutrun.py <jobs.json> <results.json> [workers]` | Real pytest on mutants in isolated copies; resumable. Use 1 worker for `test_execute_tpcds` (it spawns its own pool). |
| `tpcds_explain.py`, `tpcds_one.py` | Which queries the replay would run per commit; run the replay and report verdict and CPU. |
| `tpcds_subset_vs_full.py` | Subset replay vs all-99-query replay, to validate the coverage map. |
| `docker_eval.sh <data_dir> [answers.json]` | Run the built image under the scorer's flags; prints container CPU from cgroup. |
