# ---------------------------------------------------------------------------
# Stage 1: builder. Network allowed, 4 cores, 8 GB, 10 minutes, NOT CPU-charged.
# This is where all the expensive work happens: clone sqlglot at the pinned
# commit, run its full test suite once under coverage with per-test dynamic
# contexts (plus a custom tracker for non-Python fixture files), and reduce
# that into a single compact index.json. No answers are looked up by patch
# content -- this map is built purely from the pinned base commit, which the
# track's own rules call out as allowed ("coverage, imports, fixtures").
# ---------------------------------------------------------------------------
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder

WORKDIR /build

# From PyPI only (allowed source). coverage/pytest-cov/pygit2 are
# build-time-only tools -- none of them ship in the final image.
COPY requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock coverage==7.17.4 pytest-cov==7.1.0 pygit2==1.21.0

# Fetch sqlglot's own repository at the pinned commit over git's smart-HTTP
# protocol straight to github.com (no `git` binary needed -- pygit2 bundles
# libgit2 -- and no apt packages either). A plain shallow fetch of the exact
# SHA, not a full clone.
ENV SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python3 -c "import pygit2, os; \
sha = os.environ['SQLGLOT_COMMIT']; \
repo = pygit2.init_repository('/build/sqlglot-src', bare=False); \
remote = repo.remotes.create('origin', 'https://github.com/tobymao/sqlglot.git'); \
remote.fetch(refspecs=[sha], depth=1); \
commit = repo.get(sha); \
repo.checkout_tree(commit.tree); \
repo.set_head(commit.id)"

COPY build/conftest.py /build/sqlglot-src/conftest.py
COPY build/coveragerc /build/sqlglot-src/.coveragerc
COPY build/build_index.py /build/build_index.py

WORKDIR /build/sqlglot-src
ENV PYTHONPATH=/build/sqlglot-src \
    SKIP_INTEGRATION=0 \
    HOME=/tmp \
    FIXTURE_TOUCHES_OUT=/build/fixture_touches.json

# Full instrumented baseline run. Must pass cleanly -- if it doesn't, the
# index would be built on a broken signal, so fail the build rather than
# silently ship a bad map.
RUN python3 -m pytest tests/ -q --cov=. --cov-config=.coveragerc --cov-context=test \
    --cov-report= -p no:cacheprovider --timeout=180

RUN python3 /build/build_index.py /build/sqlglot-src /build/sqlglot-src/.coverage \
    /build/fixture_touches.json /build/index.json

# ---------------------------------------------------------------------------
# Stage 2: the run-time image. Exactly the base image the scorer labeled
# durations on, nothing else added. Only stdlib is needed at run time.
# ---------------------------------------------------------------------------
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# --chmod: the run container executes as user `nobody` on a read-only fs --
# make sure both files are world-readable regardless of the builder's umask.
COPY --from=builder --chmod=644 /build/index.json /app/index.json
COPY --chmod=644 selector.py /app/selector.py
CMD ["python3", "/app/selector.py"]
