"""Generate single-line mutants of sqlglot in the style of the sample commits.

usage: python tools/mutgen.py <sqlglot-src> <outdir> <count> <seed>
Writes <outdir>/commits/<id>.patch and <outdir>/meta.json. Dev-only: used to
measure the selector on unseen-style changes, never shipped.
"""
import difflib, io, json, random, re, sys, tokenize
from pathlib import Path

src = Path(sys.argv[1]); out = Path(sys.argv[2]); n = int(sys.argv[3]); seed = int(sys.argv[4])
(out / "commits").mkdir(parents=True, exist_ok=True)
rnd = random.Random(seed)
files = sorted((src / "sqlglot").rglob("*.py"))
import os
if os.environ.get("MUT_DIRS"):
    pre = tuple(os.environ["MUT_DIRS"].split(","))
    files = [f for f in files if f.relative_to(src).as_posix().startswith(pre)]
OPS = [
    (r"\band\b", "or"), (r"\bor\b", "and"), (r" == ", " != "), (r" != ", " == "),
    (r" is not None", " is None"), (r" is None", " is not None"), (r" is None", " == None"),
    (r" is not None", " != None"), (r" is (?!not)(?!None)", " == "), (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"), (r" < ", " <= "), (r" > ", " >= "), (r" <= ", " < "), (r" >= ", " > "),
    (r"\bnot ", ""), (r"\bif (?!not)", "if not "), ("INT", None), ("IDX", None),
]
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Keep in sync with the generator",
            "# Intentionally not cached", "# Mirrors the behavior of the reference implementation"]


def mutate(line):
    ops = OPS[:]; rnd.shuffle(ops)
    for pat, rep in ops:
        if pat == "INT":
            ms = list(re.finditer(r"(?<![\w.])(-?\d+)(?![\w.])", line))
            if ms:
                m = rnd.choice(ms); v = int(m.group(1)); nv = v + rnd.choice([1, -1]) if v else 1
                return line[:m.start()] + str(nv) + line[m.end():]
            continue
        if pat == "IDX":
            ms = list(re.finditer(r"seq_get\(args, (\d)\)", line))
            if ms:
                m = rnd.choice(ms); return line[:m.start(1)] + str(int(m.group(1)) + 1) + line[m.end(1):]
            continue
        ms = list(re.finditer(pat, line))
        if ms:
            m = rnd.choice(ms); return line[:m.start()] + rep + line[m.end():]
    return None


def code_lines(text):
    lines = text.splitlines(keepends=True)
    bad = set()
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
            bad.update(range(tok.start[0], tok.end[0] + 1))
    ok = [i for i, l in enumerate(lines)
          if (i + 1) not in bad and l.strip() and not l.strip().startswith("#")]
    return lines, ok


made, tries, meta = 0, 0, {}
while made < n and tries < n * 50:
    tries += 1
    f = rnd.choice(files); rel = f.relative_to(src).as_posix()
    lines, ok = code_lines(f.read_text())
    if not ok:
        continue
    kind = "comment" if rnd.random() < 0.12 else "code"
    i = rnd.choice(ok); new = lines[:]
    if kind == "comment":
        new.insert(i, re.match(r"\s*", lines[i]).group(0) + rnd.choice(COMMENTS) + "\n")
    else:
        m = mutate(lines[i].rstrip("\n"))
        if m is None or m == lines[i].rstrip("\n"):
            continue
        new[i] = m + "\n"
    try:
        compile("".join(new), rel, "exec")
    except SyntaxError:
        continue
    diff = "".join(difflib.unified_diff(lines, new, "a/" + rel, "b/" + rel, n=3))
    cid = f"m{seed}_{made:04d}"
    (out / "commits" / f"{cid}.patch").write_text(
        f"diff --git a/{rel} b/{rel}\nindex 1234567..89abcde 100644\n" + diff)
    meta[cid] = {"file": rel, "line": i + 1, "kind": kind, "old": lines[i].rstrip("\n"), "new": new[i].rstrip("\n")}
    made += 1
json.dump(meta, open(out / "meta.json", "w"), indent=1)
print(made, "mutants")
