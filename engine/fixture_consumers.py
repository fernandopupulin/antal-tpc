"""Narrow the executor's shared fixture reads on the pinned checkout."""

from hashlib import sha256
from io import StringIO
from pathlib import Path
import runpy


FILE = "tests/test_executor.py"
PREFIX = FILE + "::TestExecutor::"
TPC_H = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
TPC_DS = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
INPUTS = {
    "tests/helpers.py": "c0dc285fccd1b8bee4a451f5962118fe771de005843b56e3af4c7136f74c3ed3",
    FILE: "270a844a7b0b84045efde017135e6208ca07d7d93eba395be17c20c6e918923d",
}


def affected(root, relative, before, after):
    if relative not in (TPC_H, TPC_DS) or before is None or after is None:
        return None
    for source, digest in INPUTS.items():
        if sha256((Path(root) / source).read_bytes()).hexdigest() != digest:
            return None
    loader = runpy.run_path(str(Path(root) / "tests/helpers.py"))["load_sql_fixture_pairs"]

    def pairs(text):
        text.encode("utf-8")
        loader.__globals__["open"] = lambda *_args, **_kwargs: StringIO(text)
        return list(loader(relative))

    try:
        old, new = pairs(before), pairs(after)
    except (IndexError, ValueError):
        return None

    # Only these methods consume each list after setUpClass has loaded it.
    changed = set()
    if [(args, sql) for args, sql, _ in old] != [(args, sql) for args, sql, _ in new]:
        changed.add(PREFIX + ("test_execute_tpch" if relative == TPC_H else "test_execute_tpcds"))
    if relative == TPC_H and [(sql, expected) for _, sql, expected in old] != [
        (sql, expected) for _, sql, expected in new
    ]:
        changed.add(PREFIX + "test_optimized_tpch")
    return changed
