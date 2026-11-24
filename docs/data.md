# History and portability

## Conversation history

Selected user and assistant turns can be recorded through MCP or the CLI. Each message occupies one stable position in a task, branch and session. Reimporting the same session and positions is idempotent. Use separate session IDs for separate conversations.

```sh
satubrain record --task DEMO-17 --session cli-1 --position 0 --role user "Check the import tests."
satubrain search --task DEMO-17 --kind message "import"
satubrain history --task DEMO-17 --session cli-1
```

To import a reviewed conversation, create a UTF-8 JSONL file with one object per line. Allowed fields are `role`, `text`, optional `position` and optional UTC `created_at`. When no position is supplied, the line number is used.

```jsonl
{"role":"user","position":0,"text":"Check the import tests."}
{"role":"assistant","position":1,"text":"The import tests passed."}
```

```sh
satubrain history-import --task DEMO-17 --session cli-1 --agent codex selected.jsonl
```

Conversation capture is explicit. Opt-in hooks save only a marked handoff summary.

## JSON backup and restore

```sh
satubrain export "/path/outside/source/satubrain-backup.json"
satubrain import "/path/outside/source/satubrain-backup.json"
```

Exports are plaintext and include every scope in the selected database, along with deletion markers. Store exports outside source checkouts. Import merges records into the current database atomically. Normalized duplicates converge to the earliest recorded copy, and deletions win over older entries. If an exact note was deleted, save a revised entry to restore its information.

## Optional encrypted cross-device sync

1. Install the optional dependency with `python -m pip install '.[sync]'` on each device.
2. Choose an existing folder delivered across devices by a service or process you control, such as OneDrive, Syncthing or removable media. Use one database per device.
3. Run `satubrain sync push /path/to/shared/folder` on the first device.
4. Use the same passphrase with `satubrain sync pull /path/to/shared/folder` on the other device.
5. Run push after local changes and pull after the folder service delivers changes. Each device publishes its own encrypted snapshot.

The prompt accepts a passphrase of at least 16 characters. Use `SATUBRAIN_SYNC_PASSPHRASE` for noninteractive use with a trusted secret store. All project scopes in the selected database are included. Set up your own recurring push and pull if you need unattended transfers. The format supports 32 device files and up to 64 MiB per snapshot.

Sync uses AES-256-GCM with a fresh nonce and salt per snapshot and scrypt for passphrase derivation. The provider can see file sizes, device identifiers and update timing. Each device has its own plaintext local database. Use matching project identifiers and branch names to retrieve the same memory across devices.
