"""Repack index.json(.gz) for lazy loading (runs in the Dockerfile).

Layout: one JSON header line {"test_ids", "eager_files", "attr_reads",
"offsets": {path: [start, length]}} followed by each file entry's JSON at
its offset (relative to the end of the header line). selector.py decodes a
file's entry only when a commit needs it, instead of parsing all ~13 MB of
JSON on every run.

usage: python pack_index.py <index.json[.gz]> <out.pack>
"""
import gzip
import json
import sys


def main():
    src, out = sys.argv[1], sys.argv[2]
    raw = open(src, "rb").read()
    if src.endswith(".gz"):
        raw = gzip.decompress(raw)
    index = json.loads(raw)
    body, offsets, pos = [], {}, 0
    for path in sorted(index["files"]):
        blob = json.dumps(index["files"][path], separators=(",", ":")).encode()
        offsets[path] = [pos, len(blob)]
        body.append(blob)
        pos += len(blob)
    header = {k: v for k, v in index.items() if k != "files"}
    header["offsets"] = offsets
    with open(out, "wb") as f:
        f.write(json.dumps(header, separators=(",", ":")).encode() + b"\n")
        f.write(b"".join(body))


if __name__ == "__main__":
    main()
