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
# COPY's --chmod only covers the files themselves; the /app directory it
# auto-creates is left owned by root with no guarantee of being traversable
# by the `nobody` user the run container executes as. Create it explicitly
# first, world-readable and world-executable (traversable).
RUN mkdir -p /app && chmod 755 /app
COPY --chmod=644 index.json.gz /app/index.json.gz
COPY --chmod=644 selector.py /app/selector.py
CMD ["python3", "/app/selector.py"]
