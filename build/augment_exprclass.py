"""Fold build/exprclass_plugin.py's output into index.json as "clsusers".

For every class in the tracked hierarchies (Expr, Generator, Parser,
Tokenizer, Dialect, TypeAnnotator) defined in sqlglot, store the tests that
instantiated that class or any class inheriting from it (a declaration such
as arg_types or SET_OP_LIMITS on class X is inherited by X's subclasses
unless they override it), keyed by the defining file and the class's
qualname (matching index "class:<qualname>" keys; the bare name is added as
an alias when unambiguous, since the index names some nested classes that
way). Classes used by >= ALL_FRACTION of the suite are stored as "*" (the
selector treats that as select-all), which keeps the index small.

Usage:
  python build/augment_exprclass.py /path/to/sqlglot-src /tmp/exprmap index.json
"""

import glob
import importlib
import json
import os
import pkgutil
import sys

ALL_FRACTION = 0.9


def main() -> None:
    src = os.path.realpath(sys.argv[1]) + "/"
    map_dir = sys.argv[2]
    index_path = sys.argv[3]
    sys.path.insert(0, src)

    usage: dict[str, set[str]] = {}
    for f in glob.glob(os.path.join(map_dir, "*.json")):
        for nid, keys in json.load(open(f)).items():
            for k in keys:
                usage.setdefault(k, set()).add(nid)

    import sqlglot
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.optimizer.annotate_types import TypeAnnotator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(m.name)
        except Exception:  # noqa: BLE001
            pass

    index = json.load(open(index_path, encoding="utf-8"))
    test_pos = {tid: i for i, tid in enumerate(index["test_ids"])}
    n_tests = len(index["test_ids"])

    def all_subclasses(cls):
        seen, stack = set(), [cls]
        while stack:
            c = stack.pop()
            for s in c.__subclasses__():
                if s not in seen:
                    seen.add(s)
                    stack.append(s)
        return seen

    def key(c):
        return f"{c.__module__}:{c.__qualname__}"

    per_file: dict[str, dict] = {}
    aliases: dict[tuple[str, str], list] = {}
    tracked = set()
    for base in (Expr, Generator, Parser, Tokenizer, Dialect, TypeAnnotator):
        tracked |= all_subclasses(base) | {base}
    for cls in tracked:
        mod = sys.modules.get(cls.__module__)
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        path = os.path.realpath(path)
        if not path.startswith(src):
            continue
        rel = path[len(src):]
        tests: set[str] = set(usage.get(key(cls), ()))
        for sub in all_subclasses(cls):
            tests |= usage.get(key(sub), set())
        idxs = sorted(test_pos[t] for t in tests if t in test_pos)
        value = "*" if len(idxs) >= ALL_FRACTION * n_tests else idxs
        per_file.setdefault(rel, {})[cls.__qualname__] = value
        bare = cls.__qualname__.rsplit(".", 1)[-1]
        if bare != cls.__qualname__:
            aliases.setdefault((rel, bare), []).append(value)

    for (rel, bare), values in aliases.items():
        if len(values) == 1 and bare not in per_file[rel]:
            per_file[rel][bare] = values[0]

    for path, fi in index["files"].items():
        fi.pop("exprcls", None)
        fi.pop("clsusers", None)
        if path in per_file and fi["kind"] == "python":
            fi["clsusers"] = per_file[path]

    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))
    n_cls = sum(len(v) for v in per_file.values())
    n_star = sum(1 for v in per_file.values() for x in v.values() if x == "*")
    n_empty = sum(1 for v in per_file.values() for x in v.values() if x == [])
    print(f"clsusers: {n_cls} classes in {len(per_file)} files ({n_star} '*', {n_empty} unused)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
