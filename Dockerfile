# index.json.gz is a gzip-compressed, precomputed test-selection map built from the pinned
# sqlglot commit (eb11aec4be8cb2122afe713a4816662c3f0fb2fa): per-line test
# coverage (via coverage.py contexts), a custom tracker for non-Python
# fixture files, and function/class/module-level fallback ranges for lines
# that only ever execute once at import time. The rules explicitly call
# this out as the intended shape: "clone sqlglot in the Dockerfile and
# commit maps as plain files" rather than rebuild it on every image build.
# See build/ for exactly how it was produced (build/build_index.py +
# build/conftest.py), and the top-level README for how to regenerate it.
#
# The scorer is linux/amd64. Its own Python/SQLite decide which sqlglot
# tests run, so we keep the exact base image it labeled durations on.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The pinned sqlglot, for the run-time test_execute_tpcds check (see
# selector.py, "run-time test_execute_tpcds check"). sqlglot's own
# repository at the pinned commit; the one package from requirements.lock
# sqlglot imports (guarded) is installed so it behaves as when labeled.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ADD https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz /tmp/sqlglot.tar.gz
RUN pip install --no-cache-dir mypy_extensions==1.2.0

# COPY's --chmod only covers the files themselves; the /app directory it
# auto-creates is left owned by root with no guarantee of being traversable
# by the `nobody` user the run container executes as. Create it explicitly
# first, world-readable and world-executable (traversable).
RUN mkdir -p /app && chmod 755 /app
COPY --chmod=644 build/tpcds_common.py build/tpcds_prep.py build/pack_index.py /app/build/
COPY --chmod=644 index.json.gz /app/index.json.gz

# Build-time only (uncharged): repack the index for lazy loading
# (build/pack_index.py), parse the TPC-DS tables the way the test's
# setUpClass does, record every query's result at the pinned commit (under
# two hash seeds; a query whose result depends on hash order is never
# trusted), then keep only the sqlglot package itself, precompiled so the
# run-time check imports it from bytecode.
RUN set -eux; \
    mkdir -p /opt/src; \
    tar -xzf /tmp/sqlglot.tar.gz -C /opt/src --strip-components=1; \
    rm /tmp/sqlglot.tar.gz; \
    python /app/build/pack_index.py /app/index.json.gz /app/index.pack; \
    python /app/build/tpcds_prep.py /opt/src /app/spec; \
    mkdir -p /app/spec/src; \
    mv /opt/src/sqlglot /app/spec/src/sqlglot; \
    rm -rf /opt/src /app/build; \
    python -m compileall -q /app/spec/src/sqlglot; \
    chmod -R a+rX /app

COPY --chmod=644 build/tpcds_common.py build/tpcds_cov.json.gz /app/spec/
COPY --chmod=644 selector.py /app/selector.py
CMD ["python3", "/app/selector.py"]
