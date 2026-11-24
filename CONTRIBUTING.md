# Contributing to SatuBrain

Contributions are welcome! Start with a small reproducible issue or discuss a larger change before opening a PR.

## Local development

Requires Python 3.11+ with SQLite FTS5.

```sh
python -m venv .venv
```

Activate the environment (`.venv/bin/activate` on Unix shells, `.venv\\Scripts\\Activate.ps1` on PowerShell), then:

```sh
python -m pip install -e '.[sync,test]'
python -m unittest discover -s tests -v
satubrain doctor
satubrain demo
```

Use synthetic fixtures and temporary databases. Never include real transcripts, credentials, private repository paths or proprietary code in issues, screenshots, commits or test data. Report security issues privately as described in SECURITY.md.

Include tests and documentation for behavior changes. Describe what you tested and what remains unverified. Keep the core local-first and avoid mandatory external services.
