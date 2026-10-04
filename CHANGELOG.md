# Changelog

Notable changes are documented here. The helper and skill follow semantic
versioning; the bridge protocol has its own version, reported by `status`.

## Unreleased

## 1.0.0 - 2026-10-04

Initial release.

- Read actions: `status`, `review`, `list` (Inbox, Today, Upcoming, Anytime,
  Someday, Logbook), `search`, `get`, `projects`, `areas`, `tags`, and
  `completion_stats`, served from an in-memory snapshot of the Things 3
  database.
- Confirmed changes: `add_todo`, `add_project`, `update`, `complete`,
  `cancel`, `reopen`, and `move`, gated by a single-use preview nonce and a
  native macOS confirmation dialog, then applied through AppleScript.
- Read policy by area, project, tag, or `unfiled`, with a blocklist (standard)
  or default-deny allowlist (hardened, root-owned).
- Write policy: `confirm` (default, native dialog), `preview` (preview nonce
  and chat approval only, no dialog; ignored by hardened installs), or
  `disabled`.
- Redaction of verification codes, card numbers, and SSNs.
- Signed Full Disk Access wrapper with environment scrubbing and code
  validation; standard and hardened installers; doctor and allowlist tools.
- Bridge protocol 1.0.
