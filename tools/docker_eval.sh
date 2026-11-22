#!/bin/sh
# Run the built image on a data dir under the scorer's flags; print the
# score and the container's total CPU (cgroup v2 cpu.stat usage_usec).
# usage: tools/docker_eval.sh <data_dir_with_commits_and_tests.json> [answers.json]
set -e
IMAGE=${IMAGE:-blast-radius-entry}
DATA=$(cd "$1" && pwd)
OUT=$(mktemp)
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$DATA/commits":/data/commits:ro \
  --volume "$DATA/tests.json":/data/tests.json:ro \
  --entrypoint sh "$IMAGE" -c 'python3 /app/selector.py; echo "CPU_USEC $(grep usage_usec /sys/fs/cgroup/cpu.stat | cut -d" " -f2)" >&2' > "$OUT"
wc -c < "$OUT" | sed 's/^/stdout bytes: /'
if [ -n "$2" ]; then python3 score.py "$DATA" "$2" "$OUT"; fi
cp "$OUT" "${OUTPUT:-output.jsonl}"; rm -f "$OUT"
