# Security

This document describes what the Things helper can do with the permissions you
grant it, the boundaries it relies on, and how to report a vulnerability. It is
deliberately specific about limitations. If something below is too permissive
for your threat model, do not install the helper.

## Components

| Component | Source | Role |
|---|---|---|
| `grokbot-things-helper` | `bin/things_helper.c` | Signed launcher that holds the Full Disk Access grant |
| `helper.py` | `bin/helper.py` | Worker: snapshot, policy, redaction, actions, AppleScript |
| `write_gate.py` | `bin/write_gate.py` | Single-use confirm nonces for changes |
| `grokbot-things-confirm` | `bin/confirm_things_change.m` | Native approval dialog |
| LaunchAgent | `com.mcassano.grokbot-things.plist.template` | Runs the wrapper when a request file appears |

## Permissions and what they really grant

### Full Disk Access

Things 3 stores its database inside an app group container
(`~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac/`), which
macOS protects. Full Disk Access is the only practical grant that lets a
background agent read it, and it is coarse: a process holding it can read
essentially every file your user can, including other apps' protected data.

The worker only opens the Things database (plus its own bridge files and the
Things `Info.plist`), but **a bug in, or replacement of, the worker becomes a
read-anything primitive.** Treat that as the blast radius.

### The grant pins the wrapper, not Python

macOS attaches the grant to the wrapper's code signature. The wrapper therefore:

- accepts no arguments and replaces the inherited environment with a fixed
  minimal one (only a numeric `THINGS_SNAPSHOT_MAX_MB` is forwarded);
- executes exactly one interpreter and one script, both compiled in;
- validates `helper.py`, `write_gate.py`, and the confirmation binary before
  running: regular file, not a symlink, expected owner, not group- or
  world-writable;
- runs Python with `-I` (isolated: no `PYTHON*` variables, no user site
  directory, no script directory on `sys.path`) and `-B` (no bytecode writes);
- is signed with the hardened runtime, which blocks `DYLD_*` library injection.

**Standard install:** code lives in your user-owned checkout. Any process
running as you can replace `helper.py` with another user-owned, mode-500 file
and inherit Full Disk Access through the wrapper. Standard mode protects
against accidents and other user accounts, not against malware running as you.

**Hardened install:** code lives under
`/Library/Application Support/GrokBotThings/users/<uid>/libexec`, owned by
root, and the wrapper is compiled to require root ownership of everything it
loads. A process running as you cannot swap the code without administrator
rights. Administrator or root compromise is out of scope.

### Automation → Things3

The first approved change prompts you to let `grokbot-things-helper` control
Things3. That grant allows any AppleScript the helper sends to Things,
including actions this project never uses (for example emptying the Trash).
The helper only sends scripts built from a fixed set of templates, after the
confirmation gate below.

## Trust boundary: the bridge folder

Grok and the helper communicate through a private bridge folder
(`control/requests`, `control/responses`, `nonces/`, `policy/`).

**It protects against:** other user accounts (everything is mode `700`/`600`)
and sandboxed apps without access to your home folder.

**It does not protect against** unsandboxed processes running as your user
(a malicious package install, a compromised CLI tool). Such a process can:

- write read requests and collect any response the read policy allows. In
  standard mode that is everything not blocklisted; in hardened mode it is
  limited to the root-owned allowlist;
- request a change preview, take the nonce, and submit the change. With the
  default `confirm` write policy it still **cannot apply the change
  silently**: the native dialog shows the item and every field, and only a
  deliberate click on **Apply** proceeds. Cancel any dialog you did not
  expect. With the opt-in `preview` write policy (standard installs only)
  there is no dialog, and **such a process can change Things silently**.

Read requests are not tied to an interactive session. Do not allow scopes whose
disclosure to another local process would be unacceptable.

### Runtime file handling

The worker never trusts paths it did not create:

- The bridge is opened one component at a time with `O_NOFOLLOW`; every later
  operation is relative to verified directory descriptors.
- Runtime directories must be owned by you with no group/world bits. Unsafe
  objects (symlinks, FIFOs, foreign owners, permissive modes) are rejected,
  never "fixed" with `chmod`.
- Requests must be regular files you own, at most 64 KiB, named
  `request-<stem>.json`. Response names derive only from that filename stem,
  never from JSON content.
- Responses are written atomically, mode `600`, and deleted by the client after
  parsing; the helper reaps any left behind after one hour.
- The log never contains titles, notes, or checklist text, and rotates at 1 MiB.
- One worker drains a bridge at a time (an advisory lock), so a long-running
  confirmation dialog cannot cause requests to be processed twice.

## Change confirmation gate

Every change passes three layers, enforced inside the helper:

1. **Nonce.** `change_preview` normalizes and validates the change (types,
   lengths, control characters, existence of ids and tags, item state, and the
   read policy for both the target and any destination), then mints a random
   nonce bound to the SHA-256 of the canonical change. The record lives in
   `nonces/` (mode `600`) and expires after 120 seconds. `change` atomically
   claims the nonce file, so concurrent attempts cannot both succeed, and
   deletes it whether validation passes or fails, so a rejected nonce cannot be
   retried. Missing, forged, replayed, expired, or mismatched nonces are
   rejected before any dialog appears.
2. **Native dialog.** The helper shows the headline, the current item and its
   project/area, every field being changed, and the complete notes text.
   Cancel is the default (Return key); **Apply** must be clicked. Closing,
   cancelling, or 60 seconds of inaction aborts. Any unexpected dialog result
   fails closed.
3. **Constrained AppleScript.** The script is assembled from fixed templates.
   Strings are validated (no control characters other than newline and tab in
   notes, none at all in titles) and escaped as AppleScript literals; dates are
   built from integers without locale-dependent parsing; list targets come from
   a fixed table of Things list ids. The script is passed on standard input,
   never on a command line visible to other users.

The nonce is not an authorization boundary against a process that can both
write and read the bridge, because that process can request its own preview.
It prevents blind, replayed, and payload-swapped changes; the native dialog is
what prevents silent ones.

### Opting out of the dialog

`policy/write_policy.txt` set to `preview` skips layer 2. Layers 1 and 3 and
the assistant's in-chat confirmation still apply, but nothing on the Mac
requires a human, so any process running as you can make changes. The file is
user-writable, so hardened installs (which exist to resist same-user
processes) ignore `preview` and always show the dialog. `status` reports the
effective policy, `change_preview` returns `dialog_required`, and each applied
change records `approval` (`dialog` or `preview`).

## Read policy

- **Blocklist** (`policy/blocked_scopes.txt`): always enforced, in every mode,
  for reads and changes. In standard mode it is user-editable and therefore a
  guard against accidental disclosure, not a security control against a
  same-user attacker.
- **Allowlist**: in hardened mode the wrapper bakes in allowlist mode and the
  path to a root-owned, mode-600 file (readable by you through an ACL). A
  missing, symlinked, writable, or non-root file causes the wrapper or worker
  to fail closed. Change it only with `configure_allowlist.py`, which uses
  `sudo`.
- An untrusted blocklist (symlink, foreign owner, group/world-writable) makes
  the worker fail closed with nothing visible, because ignoring it could
  disclose what you meant to hide. An unrecognized `read_policy.txt` value
  falls back to allowlist mode; an unrecognized or untrusted
  `write_policy.txt` disables changes.
- `write_policy.txt` accepts `confirm` (default), `preview`, or `disabled`.

Scopes are inherited: a blocked area hides its projects and their to-dos; a
blocked tag hides items tagged with it or any child tag, including tags
inherited from a project or area. Hidden items are indistinguishable from
nonexistent ones in every response.

## Redaction

Before titles, notes, or checklist items are written to a response, the helper
masks verification codes near keywords like "code" or "OTP", 13–19 digit
card-like runs, and `NNN-NN-NNNN` SSNs. Redaction is regex-based and
incomplete (API keys, account numbers, passwords, and addresses pass through).
Do not store secrets in Things, and use the read policy as your real boundary.
Values shown in the confirmation dialog are deliberately unredacted so you see
exactly what will be written.

## The database snapshot

Each request that needs data opens the Things database read-only (no
`immutable=1`, since Things may be writing) and copies it with SQLite's online
backup API into an in-memory database, which is discarded when the request
finishes. Nothing is written to disk. A same-user process with debugging rights
could read the helper's memory, but such a process can already read the
database directly. Databases larger than 512 MB (database plus WAL) are refused
unless you raise `THINGS_SNAPSHOT_MAX_MB`.

## What leaves your Mac

The helper makes no network connections, sends no telemetry, and never
updates itself. Content Grok reads is sent to xAI as part of your
conversation and handled under xAI's terms. Limit what Grok can read with the
read policy.

## Durability

The helper depends on two interfaces neither Apple nor Cultured Code
guarantees: the Things SQLite schema and Things' AppleScript dictionary. The
worker validates required columns and fails with
`unsupported Things database schema` rather than guessing; AppleScript errors
are returned verbatim.

## Auditing an installation

- All code that runs is in this repository: read `bin/helper.py`,
  `bin/write_gate.py`, `bin/things_helper.c`, and `bin/confirm_things_change.m`.
- Release archives come with `SHA256SUMS`; tags are signed.
- Confirm `~/Library/LaunchAgents/com.mcassano.grokbot-things.plist` runs only
  the expected wrapper path with no arguments.
- `tools/doctor.py` checks ownership, modes, signatures, and the agent.

## Revoking access

1. Run `./uninstall.sh` or `./uninstall-hardened.sh`.
2. Remove `grokbot-things-helper` from **Full Disk Access** and **Automation**
   in System Settings → Privacy & Security.
3. Delete the bridge folder.

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private
[vulnerability reporting](../../security/advisories/new) for this repository,
with a description and, if possible, a minimal reproduction. You will get an
acknowledgement within a few days, and disclosure will be coordinated with you.
