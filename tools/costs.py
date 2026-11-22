"""Cost breakdown: python tools/costs.py <tests.json> <out.jsonl> [answers.json|meta.json] [top]"""
import json, sys
tests = {t["number"]: round(t["seconds"] * 1000) for t in json.load(open(sys.argv[1]))["tests"]}
extra = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else {}
ans = extra.get("commits") if "commits" in extra else None
rows, misses = [], 0
for line in open(sys.argv[2]):
    o = json.loads(line); ws = [w for w in o["workers"] if w]
    sel = {x for w in ws for x in w}
    cost = (max(sum(tests[x] for x in w) for w in ws) + 2000 * len(ws)) if ws else 0
    miss = ""
    if ans is not None:
        m = set(ans[o["commit"]]["failing"]) - sel
        if m:
            misses += 1; cost += 300000; miss = f"MISS{sorted(m)}"
    rows.append((cost / 1000, o["commit"], len(sel), miss))
rows.sort(reverse=True)
tot = sum(r[0] for r in rows)
print(f"total {tot:.3f} mean {tot / len(rows):.3f} n {len(rows)} misses {misses}")
for c, cid, n, miss in rows[: int(sys.argv[4]) if len(sys.argv) > 4 else 15]:
    m = extra.get(cid, {}) if ans is None else {}
    print(f"{c:7.2f} {n:5} {cid} {miss} {m.get('file','')}:{m.get('line','')} {m.get('old','').strip()[:50]} -> {m.get('new','').strip()[:50]}")
