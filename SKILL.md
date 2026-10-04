---
name: things-grok-bot
description: >
  Read, triage, search, and (with the user's confirmation) change Things 3
  to-dos and projects on macOS through the local Things helper bridge. Use
  when the user asks what is due, overdue, or on Today; wants to review the
  Inbox or Upcoming; searches their tasks; asks about a project or area;
  wants completion stats; or asks to add, complete, cancel, reopen,
  reschedule, retitle, tag, annotate, or move a to-do or project. macOS only.
version: 1.0.0
---

# Things 3 on macOS — Grok Bot

## When to use this skill

Use it when the user asks to:

- See Today, This Evening, the Inbox, Upcoming, Anytime, Someday, or the Logbook
- Triage: "what's overdue?", "what's due this week?", "plan my day"
- Find to-dos by text, project, area, or tag
- Inspect a project (headings, remaining to-dos) or an area
- Report throughput ("how many things did I finish this month?")
- Add a to-do or project, or complete / cancel / reopen / reschedule / rename /
  re-tag / annotate / move one

## How it works

```
  Grok Bot (you)                     macOS (the user's Mac)
  --------------                     ----------------------
  writes request-<id>.json  -->  launchd watches control/requests/  -->
  reads  response-<id>.json <--  grokbot-things-helper (Full Disk Access)
                                   reads an in-memory snapshot of the Things DB;
                                   changes go through a native confirm dialog
                                   and AppleScript
```

You exchange JSON files with a launchd agent through a **bridge folder**.
Reads never modify Things. Changes are two-step (`change_preview`, then
`change`) and the user must also click **Apply** in a macOS dialog.

## Setup and discovery

The helper must already be installed (see the repository README). If it is
not, walk the user through `./install.sh`, then granting **Full Disk Access**
to the wrapper path the installer prints.

Ask once for the bridge folder and remember it for the conversation:

> "What bridge folder did the Things installer print? (Standard installs use
> the repository folder; hardened installs use
> `~/Library/Application Support/GrokBotThings`.)"

Then call `status` first:

- Require `protocol_version` major `1`. Otherwise stop and ask the user to
  update the helper and skill together.
- If `checks.things_db_readable` is false, Full Disk Access is missing or
  stale: ask the user to (re)grant it to the wrapper path and retry.
- If `read_policy.mode` is `allowlist` and `allowlist_entries` is `0`, nothing
  is readable by design. Explain how to allow scopes (see Privacy policy);
  never change policy files yourself.
- If `write_policy` is `disabled`, do not offer to change anything.

## Making a request

Use this exact pattern. It writes atomically (temp file + rename, so launchd
never sees a partial file), waits for the response, and **deletes the
response after reading it** (responses can contain private task text).

```bash
things_request() {  # usage: things_request <bridge> <action> '<params-json>'
  /usr/bin/python3 - "$1" "$2" "${3:-{\}}" <<'PY'
import json, os, sys, time, uuid
bridge, action, params = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
rid = uuid.uuid4().hex[:16]
req_dir = os.path.join(bridge, "control", "requests")
tmp = os.path.join(req_dir, f".request-{rid}.json.tmp")
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({"id": rid, "action": action, "params": params}, f)
os.rename(tmp, os.path.join(req_dir, f"request-{rid}.json"))
resp = os.path.join(bridge, "control", "responses", f"response-{rid}.json")
deadline = time.time() + (120 if action == "change" else 20)
while not os.path.exists(resp):
    if time.time() > deadline:
        sys.exit(f"timed out waiting for {resp}; check control/log.txt")
    time.sleep(0.25)
with open(resp, encoding="utf-8") as f:
    print(f.read())
os.unlink(resp)
PY
}

BRIDGE="$HOME/Library/Application Support/GrokBotThings"   # use the user's path
things_request "$BRIDGE" status
things_request "$BRIDGE" review '{"days_ahead": 7}'
```

Every response has `ok`. On failure, `error` explains why; surface it plainly.
Responses typically arrive in 1–3 seconds. A `change` waits for the user's
dialog click, so allow up to two minutes.

## Read actions

| Action | Params (all optional unless noted) | Returns |
|---|---|---|
| `status` | — | versions, policy, install checks (no task data) |
| `review` | `days_ahead` (0–90, default 7), `limit` | `overdue`, `due_soon`, `today`, `inbox`, `counts` |
| `list` | **`list`**: `inbox` `today` `upcoming` `anytime` `someday` `logbook`; `days` (upcoming/logbook window), `limit`, `area`, `project`, `tag`, `include_notes` | `items` in Things order |
| `search` | **`term`**, `include_completed`, `limit`, `area`, `project`, `tag` | `items` with `matched_in` (`title`/`notes`/`checklist`) |
| `get` | **`id`** of a to-do, project, or area; `include_completed` | full detail: notes, checklist, headings, child to-dos |
| `projects` | `area`, `include_someday`, `include_completed`, `limit` | `projects` with `progress` |
| `areas` | — | `areas` with open counts |
| `tags` | — | `tags` with parent and usage counts |
| `completion_stats` | `days` (1–365, default 30) | completed/canceled/created counts, per-day `series`, `by_area`, lead time |

`area`, `project`, and `tag` filters accept an id or an exact (case-insensitive)
name. Tag filters include child tags.

Each item has `id`, `type` (`to-do`/`project`), `title`, `status`, `when`
(`inbox`, `today`, `evening`, `anytime`, `someday`, or a `YYYY-MM-DD` start
date), `deadline`, `tags`, `project`, `area`, optional `heading`,
`checklist` counts, `overdue`, and `url` (a `things:///show?id=…` link the user
can click).

Presentation tips: group by `when` or project, lead with overdue and due-soon
items, show deadlines as relative days, and offer the `url` when the user wants
to jump into Things. Do not dump raw JSON.

## Changing Things (preview → confirm → apply)

Never apply a change the user did not ask for. Every time:

1. Resolve ids with read actions (`search`, `get`, `projects`, `areas`). If a
   name matches several items, ask which one.
2. Call `change_preview` with the operation. Show the user `summary.headline`,
   each row of `summary.rows`, and any `summary.details` (full notes text).
3. Wait for the user's explicit "yes" in chat.
4. Call `change` with **exactly the same parameters** plus the
   `confirm_nonce` from the preview. The nonce is single-use, bound to the
   normalized change, and expires after `confirm_nonce_ttl_seconds` (120s).
   If it expires, or the user edits the change, preview again.
5. Tell the user a macOS dialog is waiting for them to click **Apply**
   (Cancel is the default; it times out after 60s).
6. On success, report `applied.operation` and offer `applied.url`.

| `operation` | Fields |
|---|---|
| `add_todo` | **`title`**, `notes`, `when`, `deadline`, `tags`, and one of `project` / `area` (ids) |
| `add_project` | **`title`**, `notes`, `when`, `deadline`, `tags`, `area` |
| `update` | **`id`** plus at least one of `title`, `notes` (replace), `append_notes`, `when`, `deadline` (`null` clears it), `tags` (replaces all; `[]` clears) |
| `complete`, `cancel`, `reopen` | **`id`** |
| `move` | **`id`** and exactly one of `project` / `area` |

- `when`: `today`, `tomorrow`, `anytime`, `someday`, `inbox` (to-dos only), or
  `YYYY-MM-DD`. Relative values become a concrete date in the preview.
  `evening` is not supported by Things' AppleScript interface; use `today`.
- `deadline`: `YYYY-MM-DD`.
- `tags` must already exist in Things (use `tags` to list them).
- Checklist items, headings, reminders, and repeating rules cannot be created
  or edited; tell the user to do those in Things.

Example:

```bash
things_request "$BRIDGE" change_preview '{"operation": "add_todo", "title": "Renew passport", "when": "tomorrow", "deadline": "2026-11-15", "tags": ["Errands"]}'
# ...show the summary, get a yes...
things_request "$BRIDGE" change '{"operation": "add_todo", "title": "Renew passport", "when": "tomorrow", "deadline": "2026-11-15", "tags": ["Errands"], "confirm_nonce": "<from preview>"}'
```

## Privacy policy

The helper filters every response before writing it:

- **Blocklist** (default, standard install): items in blocked areas, projects,
  or tags are removed entirely and cannot be changed.
- **Allowlist** (hardened install, or by choice): only allowed scopes are
  visible. `unfiled` covers to-dos with no project or area (including the Inbox).

Hidden items are indistinguishable from missing ones ("no visible item"). Do
not try to work around the policy, and never edit policy files; tell the user
which file or command to use (see the README).

Verification codes, card numbers, and SSN-shaped strings are redacted in
responses. Redaction is best-effort; the policy is the real boundary.

## Troubleshooting

| Symptom | Likely cause | What to tell the user |
|---|---|---|
| No response within ~20s | Agent not loaded, or wrapper blocked | Run the doctor command from the README; check `control/log.txt` |
| `Things database not found` / `things_db_readable: false` | Full Disk Access missing or stale after a rebuild | Re-add `grokbot-things-helper` under Full Disk Access |
| `Things AppleScript failed ... not allowed` | Automation permission denied | System Settings → Privacy & Security → Automation → enable Things3 for `grokbot-things-helper` |
| `change cancelled by user or timed out` | Dialog cancelled or ignored | Ask whether to try again |
| `confirm_nonce expired` / `change differs from preview` | Too slow, or parameters changed | Preview again |
| `unsupported Things database schema` | A Things update changed the database | Ask the user to update this helper |

## Limits

- Things 3 for Mac only; reads the local database, so unsynced changes from
  other devices appear once Things syncs on this Mac.
- Repeating templates are hidden; created repeat instances appear normally.
- No deleting, emptying the Trash, checklist editing, headings, or reminders.

See `docs/PROTOCOL.md` for the full protocol and `SECURITY.md` for the threat model.
