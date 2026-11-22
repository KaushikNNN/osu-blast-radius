"""v1-lite: run every test on every commit, but use the cheapest number of workers."""

import json
import math
import sys
from pathlib import Path

DATA = Path("/data")
MAX_WORKERS = 8
WORKER_MS = 2000


def split(tests: list[dict], worker_count: int) -> list[list[int]]:
    """Longest test first, each onto the worker with the least to do so far."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for test in sorted(tests, key=lambda t: t["ms"], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(test["number"])
        loads[lightest] += test["ms"]
    return workers


def cost(workers: list[list[int]], ms: dict[int, int]) -> int:
    return max(sum(ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    for t in tests:
        t["ms"] = math.floor(t["seconds"] * 1000 + 0.5)
    ms = {t["number"]: t["ms"] for t in tests}
    workers = min((split(tests, k) for k in range(1, MAX_WORKERS + 1)), key=lambda w: cost(w, ms))
    for patch in sorted((DATA / "commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"{len(workers)} workers, {len(tests)} tests on every commit", file=sys.stderr)


if __name__ == "__main__":
    main()
