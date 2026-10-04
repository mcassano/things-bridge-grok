# Things Bridge for Grok

> **Triage, search, and update [Things 3](https://culturedcode.com/things/)** from
> Grok Bot through a small, locally built macOS helper.

This repository provides a **Grok Bot skill** plus an on-device helper that
lets Grok read your Things 3 database and, with your explicit approval,
create and update to-dos and projects. A launchd agent answers JSON requests
from a private bridge folder: reads come from an in-memory snapshot of the
Things database, and every change requires a preview, your approval in chat,
**and** a click in a native macOS confirmation dialog before a short
AppleScript drives Things.

The helper makes no network requests. Anything Grok reads is processed by xAI
as part of your conversation, so review [SECURITY.md](./SECURITY.md) and set a
read policy before connecting work or sensitive lists.

This is an independent open-source project. It is not made, endorsed, or
supported by Cultured Code or xAI. "Things" is a trademark of Cultured Code
GmbH & Co. KG. The architecture follows the
[iMessage helper for Grok](https://github.com/jeffhuber/grokbot-imessage-skill).

---

## What it does

- **Triage** — "What's overdue or due this week?" → overdue, due-soon,
  Today/This Evening, and Inbox buckets.
- **Lists** — Inbox, Today, Upcoming, Anytime, Someday, and Logbook, in
  Things' own order, filterable by area, project, or tag.
- **Search** — titles, notes, and checklist items, optionally including the
  Logbook.
- **Inspect** — a project's headings and remaining to-dos, an area's projects,
  or a to-do's full notes and checklist.
- **Stats** — completions per day, by area and project, and lead time.
- **Change, with confirmation** — add to-dos and projects; complete, cancel,
  or reopen; reschedule; rename; replace or append notes; set or clear
  deadlines; replace tags; and move items between projects and areas.

Every item includes a `things:///show?id=…` link that opens it in Things.

---

## Requirements

| Component | Supported | Notes |
|---|---|---|
| macOS | 13 or newer; developed on macOS 26 (Apple silicon) | Uses a private database format and AppleScript, both of which Apple or Cultured Code may change. |
| Things | Things 3 for Mac; verified with 3.23 (database version 27) | The helper validates the schema and fails with a clear error if required columns disappear. |
| Python | 3.9 or newer (`/usr/bin/python3` from the Command Line Tools works) | CI runs 3.9, 3.11, and 3.13. |
| Toolchain | Xcode Command Line Tools (`xcode-select --install`) | The wrapper and dialog are compiled and signed locally. |
| Grok | Grok Build user skills (`~/.grok/skills`) or any host that can run shell commands on the Mac | The bridge protocol is host-agnostic. |

Permissions you will grant:

- **Full Disk Access** for the wrapper, because Things keeps its database in a
  protected app group container.
- **Automation → Things3** for the wrapper, the first time you approve a change.

---

## Installation (about 5 minutes)

### 1. Get the source

Download the [latest release](../../releases/latest) and verify it:

```bash
shasum -a 256 -c SHA256SUMS
```

or clone the repository:

```bash
git clone https://github.com/mcassano/things-bridge-grok.git
cd things-bridge-grok
```

### 2. Choose an installation mode

| Mode | Best fit | Trade-off |
|---|---|---|
| **Standard** (`./install.sh`) | Personal Mac, getting started | No `sudo`. Code lives in your user-writable checkout, so it does not resist a compromised process running as you. Read policy defaults to a blocklist. |
| **Hardened** (`./install-hardened.sh`) | You run other unsandboxed automation as your user, or want default-deny reads | Code is root-owned and validated by the wrapper; the read allowlist is root-owned and starts empty. Requires `sudo` and explicit allowlist maintenance. |

**Standard:**

```bash
./install.sh
```

The checkout itself becomes the bridge folder (it is set to mode `700`).

**Hardened:**

```bash
./install-hardened.sh
```

Code goes to `/Library/Application Support/GrokBotThings/users/<uid>/libexec`
(root-owned); the bridge is `~/Library/Application Support/GrokBotThings`.
Nothing is readable until you allow scopes:

```bash
CODE_ROOT="/Library/Application Support/GrokBotThings/users/$UID/libexec"
/usr/bin/python3 "$CODE_ROOT/tools/configure_allowlist.py" add "area:Work"
/usr/bin/python3 "$CODE_ROOT/tools/configure_allowlist.py" add unfiled   # Inbox and loose to-dos
/usr/bin/python3 "$CODE_ROOT/tools/configure_allowlist.py" list
```

Both installers compile the wrapper with your paths baked in, build the
confirmation dialog, sign both with the hardened runtime, install the
LaunchAgent `com.mcassano.grokbot-things`, and copy `SKILL.md` to
`${GROK_HOME:-~/.grok}/skills/things-grok-bot/`. Set `INSTALL_GROK_SKILL=0` to
skip the skill copy if your host loads `SKILL.md` another way.

### 3. Grant Full Disk Access

Open **System Settings → Privacy & Security → Full Disk Access**, click **+**,
press **Cmd-Shift-G**, paste the wrapper path the installer printed
(`…/bin/grokbot-things-helper`), and make sure its switch is on.

### 4. Verify

```bash
/usr/bin/python3 tools/doctor.py --bridge "/path/printed/by/the/installer"
```

Then run the two-minute [smoke test](./docs/SMOKE_TEST.md), which confirms
that the wrapper can actually read Things.

### 5. Use it

Ask Grok things like:

- "What's on my Things Today list, and what's overdue?"
- "Search Things for anything about the passport."
- "What's left in the Kitchen Renovation project?"
- "How many to-dos did I finish in the last 30 days, by area?"
- "Add 'Call the dentist' to Things for tomorrow with a deadline of Friday."
- "Mark 'Submit expense report' as done."

The first time you ask, Grok will ask for the bridge folder the installer
printed. The first approved change triggers a one-time macOS prompt asking
whether `grokbot-things-helper` may control Things3; click **OK**.

---

## Privacy and security

### What the helper can access

- **Full Disk Access** is coarse: macOS grants the wrapper access to all of
  your files, not just Things. The helper reads only the Things database, and
  the wrapper refuses to run anything but its own validated worker, but a bug
  in that worker has a large blast radius. See [SECURITY.md](./SECURITY.md).
- **Automation → Things3** lets the helper drive Things through AppleScript.
  It is used only after you approve a specific change in the native dialog.

### What leaves your Mac

The helper itself makes no network connections, sends no telemetry, and never
auto-updates. Task titles, notes, and checklist text that Grok reads become
part of your Grok conversation and are processed by xAI under its terms.

### Read policy

Responses are filtered **before** they are written, so hidden items never
reach Grok, and changes to hidden items are refused.

Policy files live in `<bridge>/policy/`:

| File | Purpose |
|---|---|
| `read_policy.txt` | `blocklist` (standard default) or `allowlist`. Ignored by hardened installs, which always use their root-owned allowlist. |
| `blocked_scopes.txt` | Scopes that are always hidden. Takes precedence over everything. |
| `allowed_scopes.txt` | Scopes visible in allowlist mode (standard installs only). |
| `write_policy.txt` | `confirm` (default) or `disabled` to turn off all changes. |

Scope entries, one per line:

```
area:Health          # every project and to-do in the area
project:Taxes 2026   # the project and its to-dos (an id works too)
tag:Private          # items with the tag or a child tag, including tags
                     # inherited from their project or area
unfiled              # to-dos with no project and no area (Inbox, loose items)
```

Names match exactly and case-insensitively. If a policy file is a symlink,
owned by someone else, or group/world-writable, the helper fails closed.

### Redaction

Verification codes, card-like numbers, and US SSNs are masked in titles,
notes, and checklist items. This is a best-effort backstop, not a data-loss
prevention system; the read policy is the boundary that matters.

### Changes require three approvals

1. **Preview.** `change_preview` validates the change against the current
   database and the policy, resolves relative dates, and returns a summary plus
   a single-use nonce bound to the exact normalized change (120-second TTL).
2. **Chat approval.** Grok shows you the summary and waits for your yes.
3. **Native dialog.** `change` must present the same change and nonce; the
   helper then shows a macOS dialog with the item, every field being changed,
   and the full notes text. **Cancel** is the default button; you must click
   **Apply**. The dialog times out after 60 seconds.

Only then does a short AppleScript run. Every interpolated value is either a
validated, escaped string literal, an integer, or a constant from a fixed table.

---

## Architecture

```text
  Grok Bot                              Your Mac
  --------                              --------
  request-<id>.json  ──►  control/requests/  ──► launchd (WatchPaths)
                                                      │
                                                      ▼
                                      grokbot-things-helper   (signed C wrapper,
                                                      │         Full Disk Access)
                                                      ▼
                                      python3 -I -B helper.py
                                        ├─ reads: in-memory SQLite snapshot of
                                        │  Things Database.thingsdatabase/main.sqlite
                                        ├─ read policy + redaction
                                        └─ changes: write_gate nonce
                                             → grokbot-things-confirm (dialog)
                                             → osascript → Things3
  response-<id>.json ◄──  control/responses/  (mode 600, reaped after 1 hour)
```

- The **wrapper** gives macOS a stable code identity for the Full Disk Access
  grant. It takes no arguments, discards the inherited environment, validates
  the ownership and permissions of every file it loads, and runs Python in
  isolated mode.
- The **worker** opens every runtime path relative to verified, no-follow
  directory descriptors and rejects symlinks, foreign owners, and permissive
  modes rather than repairing them.
- The **snapshot** uses SQLite's online backup API on a read-only connection,
  so it is consistent even while Things is writing, and nothing is written to disk.

Full protocol: [docs/PROTOCOL.md](./docs/PROTOCOL.md).

---

## Protocol at a glance

| Action | Purpose |
|---|---|
| `status` | Versions, policy, and install checks (reads no tasks) |
| `review` | Overdue, due soon, Today, and Inbox in one call |
| `list` | One built-in list: inbox, today, upcoming, anytime, someday, logbook |
| `search` | Text search over titles, notes, and checklists |
| `get` | Full detail for a to-do, project, or area |
| `projects`, `areas`, `tags` | Catalogs with open counts |
| `completion_stats` | Throughput and lead time over a window |
| `change_preview` | Validate a change and mint a confirm nonce |
| `change` | Apply a previewed change after native-dialog approval |

---

## Limitations

- **Things 3 for Mac only**, reading the local database. Changes made on other
  devices appear after Things syncs on this Mac.
- **No This Evening scheduling** (`when: evening`), checklist editing, headings,
  reminders, repeat rules, deletion, or emptying the Trash: Things' AppleScript
  interface does not expose them reliably. Moving a to-do *out of* a project
  into an area is also unsupported.
- **Repeating templates are hidden**; their created instances appear normally.
- Upcoming shows scheduled items; it does not project future repeats or
  calendar events.
- Depends on an undocumented database format and on AppleScript. A Things or
  macOS update can break either; the helper detects schema changes and fails
  with a clear error instead of returning wrong data.

---

## Troubleshooting

```bash
/usr/bin/python3 tools/doctor.py --bridge "/path/to/bridge"            # standard
/usr/bin/python3 "$CODE_ROOT/tools/doctor.py" --bridge "$HOME/Library/Application Support/GrokBotThings" --code-root "$CODE_ROOT"   # hardened
```

| Symptom | Cause | Fix |
|---|---|---|
| Requests pile up, no responses | Agent not loaded, or the wrapper refuses to start | Run the doctor; `launchctl print gui/$UID/com.mcassano.grokbot-things` |
| `Things database not found` or `things_db_readable: false` | Full Disk Access missing, or stale after reinstalling | Remove and re-add `grokbot-things-helper` under Full Disk Access |
| `Things AppleScript failed … not allowed` | Automation permission denied | Privacy & Security → Automation → `grokbot-things-helper` → enable Things3 |
| `unsupported Things database schema` | A Things update changed the database | Update this helper; open an issue with your Things version |
| `UnsafeRuntimePath` in the log | A bridge path is a symlink, has the wrong owner, or is group/world accessible | Remove the unexpected object or rerun the installer; do not `chmod` blindly |
| Very large database | Snapshot limit (512 MB) exceeded | Add `THINGS_SNAPSHOT_MAX_MB` to the plist's `EnvironmentVariables` and reload |

Logs are in `<bridge>/control/log.txt` (mode `600`, rotated at 1 MiB). They
record actions, timings, and errors, never titles or notes.

---

## Upgrading

Pull or unpack the new release, read [CHANGELOG.md](./CHANGELOG.md), and
rerun the installer you used originally. Rebuilding the wrapper can change its
code signature; if `status` then reports `things_db_readable: false`, remove
and re-add it under Full Disk Access.

## Uninstalling

Run `./uninstall.sh` (standard) or `./uninstall-hardened.sh` (hardened). Both
remove the LaunchAgent and the Grok skill and keep your bridge data. Then
remove `grokbot-things-helper` from **Full Disk Access** and **Automation** in
System Settings, and delete the bridge folder when you no longer need its logs.

---

## Development

```bash
./tools/test.sh            # compile checks + full unit suite
./tools/test.sh v1.0.0     # also verify release version strings
```

The suite builds synthetic Things databases, so it never touches your data.
On macOS it also compiles the wrapper against a fake interpreter to verify its
environment scrubbing and file validation, and compiles every generated
AppleScript against the Things dictionary when Things is installed.

Contributions are welcome:

1. Open an issue to discuss the change.
2. Add tests under `tests/` for any behavior change.
3. Run `./tools/test.sh`, `bash -n`, and `shellcheck` on the shell scripts.
4. Keep the helper dependency-free (standard library only).

Please report security issues privately as described in [SECURITY.md](./SECURITY.md).

## License

MIT. See [LICENSE](./LICENSE).
