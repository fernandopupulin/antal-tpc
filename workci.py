"""Adapt a baseline impact engine to a JSON plan for CI workers."""

from argparse import ArgumentParser
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import strict_diff


BASELINE = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
APP = Path(__file__).resolve().parent
ROOT = Path("/opt/sqlglot")
SUITE_CONFIG = {
    "pyproject.toml", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml",
    "setup.cfg", "setup.py", "tox.ini", "uv.lock",
}


def catalog(path):
    data = json.loads(path.read_bytes())
    tests = data["tests"]
    if (not isinstance(tests, list) or
            any(type(test.get("number")) is not int or not isinstance(test.get("id"), str)
                or not isinstance(test.get("file"), str)
                or type(test.get("seconds")) not in (int, float)
                or not 0 <= test["seconds"] < float("inf") for test in tests)):
        raise ValueError("Catálogo de testes inválido")
    if sorted(test["number"] for test in tests) != list(range(len(tests))):
        raise ValueError("Os identificadores numéricos devem ser consecutivos e únicos")
    if len({test["id"] for test in tests}) != len(tests):
        raise ValueError("O catálogo contém node IDs duplicados")
    return data, {test["number"]: test["id"] for test in tests}


def approve_patch(content):
    def read(path):
        source = (ROOT / path).resolve()
        if not source.is_relative_to(ROOT) or not source.is_file():
            return None
        return source.read_bytes()

    files = strict_diff.parse(content)
    if not files:
        raise ValueError("Diff vazio ou sem arquivos reconhecíveis")
    changes = strict_diff.apply_all(content, read)
    for file in files:
        old = read(file.old_path) if file.old_path is not None else None
        new = changes[file.new_path][1] if file.new_path is not None else None
        if strict_diff.index_matches(file, old, new) is False:
            raise ValueError("O diff diverge da revisão base")
        for path in (file.old_path, file.new_path):
            if path and (path in SUITE_CONFIG or path.startswith(("requirements", "constraints"))
                         or path.endswith("/conftest.py") or path == "conftest.py"
                         or path.startswith("tests/") and path.endswith(".py")):
                raise ValueError("O diff altera a coleta de testes ou o ambiente do pytest")


def plan(args):
    data, ids = catalog(APP / "catalog.json")
    reports = {}
    keys = [f"patch-{i}" for i in range(len(args.patch))]
    accepted = {}

    def full(key, reason):
        reports[key] = {"status": "suite-completa", "reason": reason, "workers": [list(ids.values())] if ids else []}

    for key, path in zip(keys, args.patch):
        try:
            content = path.read_bytes()
            approve_patch(content)
            accepted[key] = content
        except (OSError, ValueError, KeyError) as error:
            full(key, str(error))

    if accepted and ids:
        with TemporaryDirectory(prefix="antl-") as directory:
            folder = Path(directory)
            commits = folder / "commits"
            commits.mkdir()
            (folder / "tests.json").write_text(json.dumps(data), encoding="utf-8")
            for key, content in accepted.items():
                (commits / (key + ".patch")).write_bytes(content)
            env = {**os.environ, "BR_DATA": str(folder), "BR_VERIFY": "1" if args.verify else "0",
                   "BR_MAX_WORKERS": str(args.max_workers), "BR_WORKER_MS": str(args.worker_cost_ms),
                   "BR_BUDGET": str(max(1, args.timeout - 4)),
                   "BR_HARD": str(max(2, args.timeout - 2))}
            try:
                completed = subprocess.run(
                    [sys.executable, str(APP / "engine/selector.py")], env=env,
                    capture_output=True, check=True, timeout=args.timeout,
                )
                if args.verbose:
                    sys.stderr.buffer.write(completed.stderr)
                for line in completed.stdout.splitlines():
                    result = json.loads(line)
                    key = result["commit"]
                    workers = result["workers"]
                    if (key not in accepted or key in reports or not isinstance(workers, list)
                            or len(workers) > args.max_workers
                            or any(not isinstance(worker, list) for worker in workers)):
                        raise ValueError("Resposta inesperada do seletor")
                    selected = [number for worker in workers for number in worker]
                    if (any(type(number) is not int or number not in ids for number in selected)
                            or len(set(selected)) != len(selected)):
                        raise ValueError("O seletor devolveu IDs inválidos ou duplicados")
                    reports[key] = {
                        "status": "plano",
                        "workers": [[ids[number] for number in worker] for worker in workers],
                    }
            except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError):
                for key in accepted:
                    full(key, "O seletor não entregou uma resposta completa e válida")
    for key in accepted.keys() - reports.keys():
        full(key, "O seletor não devolveu um plano")
    result = {"baseline": BASELINE, "mode": "verificado" if args.verify else "estático", "plans": []}
    for key, patch in zip(keys, args.patch):
        entry = reports[key]
        workers = entry["workers"]
        result["plans"].append({
            "patch": str(patch), **entry,
            "test_count": sum(len(worker) for worker in workers) if entry["status"] == "plano" else None,
            "commands": ([["python", "-m", "pytest", "-q", "tests"]]
                         if entry["status"] == "suite-completa"
                         else [["python", "-m", "pytest", "-q", *worker] for worker in workers if worker]),
        })
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = ArgumentParser(description="Gera planos de teste para diffs contra a revisão base.")
    parser.add_argument("patch", nargs="+", type=Path, help="Diffs independentes contra a revisão base")
    parser.add_argument("--verify", action="store_true", help="Executa verificações de baixo custo antes do plano")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-workers", type=int, default=8, help="Máximo de jobs paralelos por diff")
    parser.add_argument("--worker-cost-ms", type=int, default=2000, help="Custo atribuído a cada job em ms")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not 10 <= args.timeout <= 300:
        parser.error("--timeout deve estar entre 10 e 300 segundos")
    if not 1 <= args.max_workers <= 32:
        parser.error("--max-workers deve estar entre 1 e 32")
    if not 0 <= args.worker_cost_ms <= 600_000:
        parser.error("--worker-cost-ms deve estar entre 0 e 600000")
    try:
        plan(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        parser.exit(2, f"antl: {error}\n")


if __name__ == "__main__":
    main()
