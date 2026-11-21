"""Start the batch engine for mounted data or the interactive CI planner."""

import os
from pathlib import Path
import sys


def main():
    app = Path(__file__).resolve().parent
    data = Path(os.environ.get("BR_DATA", "/data"))
    if len(sys.argv) == 1 and (data / "commits").is_dir() and (data / "tests.json").is_file():
        os.execv(sys.executable, [sys.executable, str(app / "engine/selector.py")])
    os.execv(sys.executable, [sys.executable, str(app / "workci.py"), *sys.argv[1:]])


if __name__ == "__main__":
    main()
