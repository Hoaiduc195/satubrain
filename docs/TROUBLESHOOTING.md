# Troubleshooting

| Symptom | What to try |
| --- | --- |
| Command not found | Verify installation and PATH; for pipx run `pipx ensurepath` and reopen the terminal. |
| SQLite FTS5 unavailable | Run `satubrain doctor`; use a Python distribution built with FTS5. |
| Agent cannot launch MCP | Use the absolute installed executable path, merge the generated configuration into the correct client file, and restart the client. |
| Agent cannot find a checkpoint | Check project ID, task, branch and database path on both clients. Run `satubrain scopes` locally. |
| Sync does not appear on another device | Verify folder delivery, matching passphrase and project ID; run push and pull explicitly. |

See [agent setup](agents.md) and [data portability](data.md). The runtime doctor does not verify a live agent connection. Never post your database, transcripts, credentials or unredacted configuration in a public issue.
