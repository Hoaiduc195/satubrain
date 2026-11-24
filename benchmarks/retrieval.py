"""Run a repeatable local benchmark with synthetic, task-scoped memory."""

from argparse import ArgumentParser
import json
from pathlib import Path
from statistics import median, quantiles
from tempfile import TemporaryDirectory
from time import perf_counter

from satubrain.store import Store


def benchmark(records, queries):
    with TemporaryDirectory() as directory:
        store = Store(Path(directory) / "bench.sqlite3")
        start = perf_counter()
        store.save_batch({
            "project": "benchmark", "task": f"task-{index % 10}", "branch": "main",
            "kind": "note", "text": f"Checkpoint {index} for module{index % 23} about migration and validation.",
        } for index in range(records))
        ingest = perf_counter() - start
        durations = []
        for index in range(queries):
            start = perf_counter()
            context = store.context("benchmark", f"task-{index % 10}", "main",
                                    f"module{index % 23} migration", 1200)
            durations.append((perf_counter() - start) * 1000)
            assert context["items"] and context["context_bytes"] <= 1200
        return {
            "records": records, "queries": queries, "ingest_seconds": round(ingest, 3),
            "context_median_ms": round(median(durations), 2),
            "context_p95_ms": round(quantiles(durations, n=20, method="inclusive")[-1], 2),
        }


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=20_000)
    parser.add_argument("--queries", type=int, default=200)
    args = parser.parse_args()
    if args.records < 10 or args.queries < 2:
        parser.error("requires at least 10 records and 2 queries")
    print(json.dumps(benchmark(args.records, args.queries), indent=2))
