# Security

SatuBrain is a single-user local application. Configure MCP only in trusted clients, and keep each database owned by the intended OS user. Use separate databases where users or environments require separate trust boundaries. Project, task and branch scopes are retrieval boundaries within a database.

## Data and trust

- SQLite and JSON exports contain plaintext. Use OS account controls and disk encryption for local data. Keep the database outside your source tree where possible.
- The dashboard binds to loopback, checks Host and Origin, and requires a per-launch token for memory access. Open the printed URL locally and treat it as a session credential. Memory is rendered as text under a restrictive content security policy.
- The inline secret check rejects common credentials in JSON, environment assignments, connection strings, URLs and authorization headers. Review text before saving. Stored text can contain prompts or private content and should be treated as reference material when passed to a model.
- Opt-in lifecycle hooks save only a marked summary. Store only material you have permission to retain.
- Encrypted snapshots use AES-GCM with scrypt passphrase derivation. An authenticated snapshot can still come from any party holding the passphrase. The folder provider can delay, omit or replay files, and backups may retain older ciphertext. Use a high-entropy shared passphrase. Keep one live SQLite database per device. [OWASP scrypt guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- Deletion removes the searchable record and syncs a deletion marker. Copies may remain in external backups, earlier exports, agent transcripts or SQLite journal history.

Report vulnerabilities privately using GitHub's private vulnerability reporting when enabled. Include the affected version, the conditions and the impact.
