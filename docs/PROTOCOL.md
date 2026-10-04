# Things Bridge Protocol

Protocol version: `1.0`

The helper and its clients exchange JSON files through a bridge folder. Call
`status` before the first operation and require protocol major version `1`.
Minor versions add backward-compatible actions or fields; a major mismatch
must fail closed with upgrade guidance.

## Transport

```
<bridge>/
├── control/
│   ├── requests/     client writes request-<stem>.json (atomically)
│   ├── responses/    helper writes response-<stem>.json (mode 600)
│   ├── log.txt       diagnostics, no task content
│   └── lock          worker lock
├── policy/           read_policy.txt, write_policy.txt, blocked_scopes.txt, allowed_scopes.txt
└── nonces/           confirm nonces (mode 700 / 600)
```

1. Write the request to `control/requests/.request-<stem>.json.tmp`, then
   rename it to `request-<stem>.json`. Files starting with `.` and names that do
   not match `request-*.json` are ignored, so launchd never processes a partial
   write.
2. launchd starts the helper within about a second (`WatchPaths`,
   `ThrottleInterval` 1). The helper drains every pending request, oldest name
   first, and keeps draining until the queue is empty.
3. Poll for `control/responses/response-<stem>.json`. It appears atomically.
4. Parse it, then **delete it**. Responses can contain private text; the helper
   deletes leftovers after one hour.

`<stem>` must be non-empty, must not start with `.`, and must not contain `/`.
Use a random value (for example 16 hex characters). The response name is derived
from the filename only, never from the JSON `id`.

Requests are limited to 64 KiB and must be regular files owned by the bridge
owner.

## Request

```json
{"id": "8f2c1e0a9b7d4c11", "action": "review", "params": {"days_ahead": 7}}
```

| Field | Type | Notes |
|---|---|---|
| `id` | string | Echoed back (truncated to 128 characters). Defaults to the stem. |
| `action` | string | One of the actions below. |
| `params` | object | Optional; defaults to `{}`. Unknown parameters are rejected. |

## Response

Success:

```json
{"id": "8f2c1e0a9b7d4c11", "ok": true, "action": "review", "generated_at": "2026-10-05T09:12:44", "...": "action fields"}
```

Failure:

```json
{"id": "8f2c1e0a9b7d4c11", "ok": false, "action": "review", "error": "days_ahead must be in [0, 90]"}
```

An unknown action also returns `allowed_actions`. Errors for hidden and
nonexistent items are identical (`no visible item with id '…'`).

## Item objects

Read actions return to-dos and projects in this shape:

```json
{
  "id": "6Xq3Lr8hM2oPz1VbJk4TcA",
  "type": "to-do",
  "title": "Renew passport",
  "status": "open",
  "when": "today",
  "start_date": "2026-10-03",
  "deadline": "2026-10-10",
  "overdue": false,
  "tags": ["Errands"],
  "project": {"id": "Fh2…", "title": "Travel prep"},
  "area": {"id": "Qp9…", "title": "Personal"},
  "heading": "Documents",
  "checklist": {"open": 1, "total": 3},
  "repeating": true,
  "created_at": "2026-09-28T18:02:11",
  "modified_at": "2026-10-03T08:15:40",
  "url": "things:///show?id=6Xq3Lr8hM2oPz1VbJk4TcA",
  "notes": "Photo booth at the post office…"
}
```

| Field | Meaning |
|---|---|
| `type` | `to-do` or `project` |
| `status` | `open`, `completed`, or `canceled` |
| `when` | `inbox`, `today`, `evening` (This Evening), `anytime`, `someday`, or a future `YYYY-MM-DD` start date |
| `start_date`, `deadline` | ISO dates or `null` |
| `overdue` | Present for open items with a deadline |
| `tags` | The item's own tags (inherited tags are used for policy, not listed) |
| `project`, `area` | `{id, title}` or `null`. For a to-do under a heading, `project` is the heading's project. A project's `area` is its own area. |
| `heading`, `checklist`, `repeating` | Present only when applicable |
| `progress` | Projects only: `{open, completed, canceled}` child to-do counts |
| `completed_at` / `canceled_at` | Present for closed items |
| `notes` | Omitted, a 280-character snippet, or full text, depending on the action |

Text fields pass through redaction (verification codes, card numbers, SSNs).

## List semantics

The helper reproduces Things' lists from the database:

| List | Contents |
|---|---|
| `inbox` | Open to-dos with start = Inbox |
| `today` | Open items whose start date is today or earlier, plus unscheduled items whose deadline has arrived; ordered like Things, This Evening last |
| `upcoming` | Open items with a start date after today, within `days` (default 30) |
| `anytime` | Open items with start = Anytime and no future start date, excluding to-dos in Someday or future-scheduled projects |
| `someday` | Open items with start = Someday and no start date |
| `logbook` | Completed or canceled items closed within the last `days` (default 7; `1` = today), newest first |

All lists exclude the Trash (including items in trashed projects or headings),
repeating templates, and to-dos inside closed projects.

## Actions

### `status`

Reads no Things content. Safe to call any time.

```json
{"id": "s1", "action": "status", "params": {}}
```

Returns `helper_version`, `protocol_version`, `host_display_name`,
`launchd_label`, `allowed_actions`, `code_root`, `bridge_root`,
`python_version`, `things_app` (`installed`, `path`, `version`, `bundle_id`),
`read_policy` (`mode`, `allowlist_entries`, `blocklist_entries`,
`root_owned_required`), `write_policy` (`confirm` or `disabled`),
`confirm_nonce_ttl_seconds`, and `checks`:

| Check | Meaning |
|---|---|
| `things_container_accessible` | The Things group container can be listed |
| `things_db_found` | A database file was located |
| `things_db_readable` | This process can read it (the real Full Disk Access test) |
| `confirmation_helper_executable` | The dialog binary is present |
| `requests_dir_exists`, `responses_dir_exists` | Bridge layout is intact |

### `review`

| Param | Default | Bounds |
|---|---|---|
| `days_ahead` | 7 | 0–90 |
| `limit` | 50 per bucket | 1–500 |

Returns `date`, `days_ahead`, `counts` (`overdue`, `due_soon`, `today`,
`evening`, `inbox`, `open_total`) and item arrays `overdue` (deadline before
today), `due_soon` (deadline today through `days_ahead`), `today`, and `inbox`,
each with note snippets. An item can appear in more than one bucket.

### `list`

| Param | Default | Notes |
|---|---|---|
| `list` | required | `inbox`, `today`, `upcoming`, `anytime`, `someday`, `logbook` |
| `days` | 30 (upcoming), 7 (logbook) | 1–365; ignored by other lists |
| `limit` | 100 | 1–500 |
| `area`, `project`, `tag` | — | Id or exact case-insensitive name; `tag` includes child tags |
| `include_notes` | `false` | Adds note snippets |

Returns `list`, `count` (before the limit), `truncated`, `days` (when
relevant), and `items`.

### `search`

| Param | Default | Notes |
|---|---|---|
| `term` | required | 1–200 characters, case-insensitive substring |
| `include_completed` | `false` | Also search completed and canceled items |
| `limit` | 50 | 1–500 |
| `area`, `project`, `tag` | — | As for `list` |

Searches titles, notes, and checklist items of to-dos and projects. Returns
`term`, `include_completed`, `match_count`, `truncated`, and `items`, each with
`matched_in` (`title`, `notes`, `checklist`). Open items come first, then most
recently modified.

### `get`

| Param | Default | Notes |
|---|---|---|
| `id` | required | A to-do, project, or area id |
| `include_completed` | `false` | Include closed children |

- **To-do:** `item` with full `notes` (up to 20,000 characters;
  `notes_truncated` if longer) and `checklist_items`
  (`[{title, completed, canceled}]`).
- **Project:** `item` (with `progress`), `headings` (`[{id, title}]` in order),
  and `todos` ordered by heading, then position.
- **Area:** `item` (`type: "area"`, `tags`, `url`), `projects`, and loose `todos`.

### `projects`

Params: `area`, `include_someday` (default `true`), `include_completed`
(default `false`), `limit` (default 500). Returns `count`, `truncated`, and
`projects` ordered by area and position.

### `areas`

No params. Returns `count` and `areas`: `[{id, title, tags, open_projects,
open_todos, url}]`. In allowlist mode an area is listed when it is allowed or
contains something visible.

### `tags`

No params. Returns `count` and `tags`: `[{id, title, parent, shortcut,
open_items}]`. Blocked tags (and children of blocked tags) are omitted; in
allowlist mode only tags on visible open items are listed.

### `completion_stats`

Param: `days` (1–365, default 30), counting today as day one.

Returns `days`, `from`, `to`, `completed`, `canceled`, `created`,
`net_change` (created − completed − canceled), `average_per_day`,
`lead_time_days` (`median`, `mean`, `sample_size`; creation to completion),
`by_area`, `top_projects` (10), and a zero-filled daily `series`. Counts cover
visible to-dos only.

### `change_preview`

Validates a change and mints a nonce. **Changes nothing.**

```json
{
  "id": "p1",
  "action": "change_preview",
  "params": {
    "operation": "add_todo",
    "title": "Renew passport",
    "when": "tomorrow",
    "deadline": "2026-11-15",
    "tags": ["errands"],
    "project": "Fh2…"
  }
}
```

Response:

```json
{
  "ok": true,
  "action": "change_preview",
  "change": {
    "operation": "add_todo",
    "title": "Renew passport",
    "when": "2026-10-06",
    "deadline": "2026-11-15",
    "tags": ["Errands"],
    "project": "Fh2…"
  },
  "summary": {
    "headline": "Grok Bot wants to add a to-do",
    "rows": [["Title", "Renew passport"], ["Project", "Travel prep"], ["When", "2026-10-06"],
             ["Deadline", "2026-11-15"], ["Tags", "Errands"]],
    "details": ""
  },
  "confirm_nonce": "Q2x…",
  "confirm_nonce_ttl_seconds": 120
}
```

`change` is the normalized form: trimmed title, canonical tag names, resolved
relative dates, and only the fields supplied. The nonce is bound to it.

#### Operations

| `operation` | Required | Optional |
|---|---|---|
| `add_todo` | `title` | `notes`, `when`, `deadline`, `tags`, `project` **or** `area` |
| `add_project` | `title` | `notes`, `when`, `deadline`, `tags`, `area` |
| `update` | `id` and at least one optional field | `title`, `notes` **or** `append_notes`, `when`, `deadline` (`null` clears), `tags` (replaces; `[]` clears) |
| `complete` | `id` (open item) | — |
| `cancel` | `id` (open item) | — |
| `reopen` | `id` (completed or canceled item) | — |
| `move` | `id` and exactly one of `project`, `area` | — |

#### Field rules

| Field | Rule |
|---|---|
| `title` | 1–500 characters after trimming; no line breaks or control characters |
| `notes`, `append_notes` | Up to 10,000 characters; newline and tab allowed; CRLF normalized |
| `when` | `today`, `tomorrow`, `anytime`, `someday`, `inbox` (to-dos outside projects only), or `YYYY-MM-DD` from today up to 10 years ahead. `evening` is rejected (not available through AppleScript). |
| `deadline` | `YYYY-MM-DD` between 2000-01-01 and 2100-12-31 |
| `tags` | Up to 20 existing tag names or ids (no commas in names) |
| `project`, `area`, `id` | Things ids of live, visible items; destination projects must be open |

Other constraints:

- Only open items can be updated or moved.
- `move` puts a to-do into a project, or a project or loose to-do into an
  area. Moving a to-do out of a project is not supported.
- The target, the destination, and the scope of a new item must all pass the
  read policy. In allowlist mode, creating an item with no project or area
  requires the `unfiled` scope.
- `write_policy.txt` set to `disabled` rejects all previews and changes.

### `change`

Same params as the preview, plus `confirm_nonce`. The helper re-validates
against the current database, verifies and consumes the nonce, shows the
native dialog, and runs the AppleScript.

```json
{"id": "c1", "action": "change", "params": {"operation": "complete", "id": "6Xq…", "confirm_nonce": "Q2x…"}}
```

```json
{
  "ok": true,
  "action": "change",
  "applied": {
    "operation": "complete",
    "id": "6Xq…",
    "url": "things:///show?id=6Xq…",
    "applied_at": "2026-10-05T09:15:02"
  }
}
```

For `add_todo` and `add_project`, `applied.id` is the new item's id.

| Error | Meaning |
|---|---|
| `missing confirm_nonce; call change_preview first` | No nonce supplied |
| `confirm_nonce not recognized; …` | Forged, already used, or burned by an earlier failure |
| `confirm_nonce expired (TTL 120s); …` | Preview again |
| `change differs from preview; …` | Parameters changed, or a relative date rolled over midnight |
| `change cancelled by user or timed out (60s dialog limit)` | The dialog was not approved |
| `Things AppleScript failed (rc=…): …` | Things rejected the script (for example, Automation permission denied) |

A nonce is consumed by the first `change` that presents it, whatever the
outcome. Wait for the response before retrying; a second worker cannot run
while a dialog is open.

## Read policy

See the README's *Read policy* section for file formats. Entries are
`area:<name or id>`, `project:<name or id>`, `tag:<name or id>`, or
`unfiled`. An item's scope is its area (directly or through its project), its
project, its own tags plus those of its project and area (with all ancestor
tags), and `unfiled` when it has neither a project nor an area. The blocklist
wins over the allowlist.
