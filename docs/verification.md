# Verification and benchmarks

## Run the checks

```sh
python -m pip install '.[sync,test]'
python -m unittest discover -s tests -v
python benchmarks/retrieval.py --records 20000 --queries 200
```

The suite covers concurrent deduplication, scoped retrieval, branch changes, bounded context, JSONL ordering, package configuration, an end-to-end stdio handoff using the official MCP SDK, native hook payloads, loopback dashboard authorization, and encrypted push, pull and delete propagation between two databases. GitHub Actions runs the suite on Linux, Windows and macOS.

The dashboard also has a browser workflow for search, inspection, adding and deleting memory, reload and task navigation on phone and desktop viewports. To reproduce it locally:

```sh
python -m pip install 'playwright>=1.53,<2'
python -m playwright install chromium
python tests/browser_dashboard.py
```

## Local performance baseline

Python 3.12.14 and SQLite 3.53.1 on a Linux development runner. The synthetic workload creates 20,000 notes across 10 tasks. Each of 200 queries opens a local database connection, retrieves context with FTS5, and enforces a 1,200-byte budget.

| Measurement | Result |
| --- | ---: |
| Batch ingest | 0.64 s |
| Context retrieval median | 5.39 ms |
| Context retrieval p95 | 6.28 ms |

Timings vary with the machine, database size and selectivity of the query. CI runs a smaller workload as a functional check.

## Release candidate scope

Version 1.0.0rc1 implements the v1.0 feature set. The official MCP SDK and documented hook fixtures cover the shared handoff flow. The Codex CLI loaded the generated stdio settings. Live agent checks remain for Claude Code, Codex, GitHub Copilot in VS Code and Cursor. Folder sync was tested between independent databases through a local transfer folder. An actual folder provider and second physical device remain to be verified. The security review is a repository self-review backed by tests and the [documented trust model](../SECURITY.md).
