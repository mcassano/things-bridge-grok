# Smoke Test

Run this after installing, upgrading, or re-granting permissions. It takes
about two minutes and does not change anything in Things until step 5, which
you can skip.

Set the bridge path the installer printed:

```bash
BRIDGE="$HOME/Library/Application Support/GrokBotThings"   # hardened default
# BRIDGE="$HOME/things-bridge-grok"                         # standard: your checkout
```

Define the same request function the Grok skill uses:

```bash
things_request() {
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
```

## 1. The agent is loaded

```bash
launchctl print "gui/$UID/com.mcassano.grokbot-things" | head -5
```

Expected: a description of the service. If it reports "Could not find
service", rerun the installer.

## 2. Status and Full Disk Access

```bash
things_request "$BRIDGE" status
```

Expected: `"ok": true`, `"protocol_version": "1.0"`, and in `checks`,
`things_db_found` and `things_db_readable` both `true`.

- No response within 20 seconds: check `$BRIDGE/control/log.txt`, then run
  `tools/doctor.py`.
- `things_db_readable: false`: Full Disk Access is missing or stale. Remove and
  re-add `grokbot-things-helper` in System Settings, then repeat.

## 3. A read

```bash
things_request "$BRIDGE" review '{"days_ahead": 7}'
```

Expected: `"ok": true` and counts that match what Things shows. In a fresh
hardened install every count is `0` until you allow scopes.

## 4. The read policy works

Pick an area you can see in step 3, block it, and confirm it disappears
(standard install; for hardened installs use `configure_allowlist.py`):

```bash
printf 'area:<Area name>\n' >> "$BRIDGE/policy/blocked_scopes.txt"
things_request "$BRIDGE" areas
```

Remove the line again afterwards.

## 5. A confirmed change (optional, creates one to-do)

```bash
things_request "$BRIDGE" change_preview '{"operation": "add_todo", "title": "Bridge smoke test", "when": "someday"}'
```

Copy `confirm_nonce` from the response, then within two minutes:

```bash
things_request "$BRIDGE" change '{"operation": "add_todo", "title": "Bridge smoke test", "when": "someday", "confirm_nonce": "<nonce>"}'
```

A dialog titled "Grok Bot wants to add a to-do" appears. Check that
**Cancel** is the default button, then click **Apply**. The first time, macOS
also asks whether `grokbot-things-helper` may control Things3; click **OK**.

Expected: `"ok": true` with `applied.id`. The to-do appears in Things under
Someday. Complete the cleanup through the bridge:

```bash
things_request "$BRIDGE" change_preview '{"operation": "cancel", "id": "<applied.id>"}'
things_request "$BRIDGE" change '{"operation": "cancel", "id": "<applied.id>", "confirm_nonce": "<nonce>"}'
```

## 6. Nothing is left behind

```bash
ls "$BRIDGE/control/requests" "$BRIDGE/control/responses"
```

Expected: both empty.
