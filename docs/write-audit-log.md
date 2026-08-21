# Write audit log — Microsoft Graph and GoHighLevel

Every write that mutates a record in Microsoft Graph (contacts, To-Do tasks) or
GoHighLevel (contacts) is preceded by a durable, append-only audit line at
`~/.hermes/logs/write_audit/YYYY-MM.jsonl`.

## The invariant

**A write never reaches its destination unless its audit line has already landed
on disk, successfully, first.**

The log append is a *precondition*, not a companion action. On every path — happy,
error, and retry — the sequence in every write method is:

```python
require_trigger(trigger)                      # reject a write with no stated reason
before = self._call("GET", path)              # a read; nothing mutated yet
authorized = self._authorize(...)             # audit line appended + fsync'd, or raises
self._call("PATCH", path, json_body=changes)  # unreachable unless the line landed
after, failed = self._fetch_after(path)       # best-effort read-back
authorized.record_outcome(after=after, after_fetch_failed=failed)
```

If the append fails for any reason — disk full, permission denied, missing
directory, a `before` payload that will not serialize — `WriteAuditLogError`
propagates to the caller and the destination API is never called. Not
called-then-rolled-back: never invoked. There is deliberately no rollback path,
because a create cannot be un-created without another API call that could also
fail.

The failure is loud by construction. The exception names the blocked destination,
the operation, the record, the actor, the trigger, and the underlying cause, e.g.:

```
BLOCKED: the msgraph_contacts update of record 'AAMkAD01' was NOT attempted — the
destination API was never called — because its write-audit line could not be
appended to /Users/x/.hermes/logs/write_audit/2026-08.jsonl:
PermissionError(13, 'Permission denied'). Actor: msgraph_contacts_client.
Trigger: 'Nathan intro triage 2026-08-21'. Fix the log path/permissions and re-run the write.
```

Nothing in this path degrades to a warning or an exit 0. A blocked write surfaces
exactly like a real API error would.

The retry loop lives *below* the gate, inside
`tools/sync_json_http.py:request_json`, so a failed append blocks the **first**
attempt, not just the last one.

## Why each write produces two lines

The format requires `after` — the object's *post-write* state. That value cannot
exist before the write, and the write cannot happen before the log line. The only
way to honour both is to append twice for one logical write:

| `audit_phase` | when | `after` |
| --- | --- | --- |
| `intent` | **before** the destination call — this is the line that gates the write | `null` |
| `outcome` | after the destination call returned successfully | populated (or flagged) |

Both lines carry the full required field set and share an `audit_id`. An intent
line with no matching outcome line is itself informative: a write was authorized
and then did not complete (destination error, crash, network loss).

## Line format

One JSON object per line:

| field | notes |
| --- | --- |
| `timestamp` | ISO8601 UTC, millisecond precision — `2026-08-21T22:15:03.412Z` |
| `destination` | `msgraph_contacts`, `msgraph_tasks`, `ghl_contacts` |
| `record_id` | destination's own ID; `null` on an intent line for a create |
| `operation` | `create`, `update`, `delete` |
| `before` | full pre-write state (`null` for create) — always a real fetch, never reconstructed from the request payload |
| `after` | full post-write state (`null` for delete, `null` on intent lines) |
| `actor` | `msgraph_contacts_client`, `msgraph_tasks_client`, `ghl_client` |
| `trigger` | free-text reason, **required** — a write with no trigger is rejected before the log and before the destination call |
| `audit_id` | links the intent and outcome lines of one write |
| `audit_phase` | `intent` or `outcome` |
| `after_fetch_failed` | present and `true` only when the post-write state could not be established |

### Read-back failure

If the destination write succeeds but the follow-up read fails, the outcome line
is still written — losing the fact that the write happened would be far worse than
losing its final state. In that case `after` is exactly
`"UNKNOWN — verify manually"` and `after_fetch_failed: true` is set, so those
entries are findable by field query rather than by string-spotting:

```
python scripts/write_audit_query.py --after-fetch-failed
```

The same flag covers the rarer case where a create's response carries no `id`:
there is then nothing to read back, so the post-write state is equally
unestablished. Either way the flag means "go look at this record by hand".

No exception propagates for this case: the write succeeded and must not be
reported as failed.

### Telling the two failure modes apart

A caller with a blanket retry handler must not treat these the same way, so both
exceptions carry a `write_completed` class attribute:

| exception | `write_completed` | meaning |
| --- | --- | --- |
| `WriteAuditLogError` | `False` | the destination never saw the write — safe to retry |
| `WriteAuditOutcomeLogError` | `True` | the write landed but its outcome line did not — retrying double-writes |

## Append-only mechanics

`tools/write_audit_log.py:append_entry` is the only writer. It:

- serializes to JSON **before** touching the file, so a bad payload cannot leave a
  partial line;
- opens with `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)` and
  then `open(fd, "a", ...)` — append at both the syscall and the file-object level;
- `flush()` + `os.fsync()` of the file on every append, exactly once;
- additionally fsyncs the containing directory on the append that *creates* a
  month file — otherwise the line's bytes would be durable while the directory
  entry pointing at them was not, and a crash could lose the first line of a month
  after its destination write had already happened;
- `os.fchmod(fd, 0o600)` so the file is never group/world readable.

No code path opens a log file with `"w"` or `"r+"`, and no code path rewrites,
truncates, renames, or removes any existing line or file. The query tool opens
files read-only.

## How long entries are kept

Forever. No function in this feature shortens, ages out, or takes away log data —
the absence is structural, not "we don't call it", so there is nothing for someone
to wire up by mistake later. `tests/test_write_audit_log.py::TestStructuralGuarantees`
enforces this by parsing the feature's source: it fails if any of the forbidden
identifiers appear, if any `open()` uses a mode other than `"a"`/`"r"`, or if any
call to `unlink`/`remove`/`rmtree`/`truncate`/`rename`/`replace` is introduced.

## Backup coverage — confirmed, no change needed

`~/.hermes/logs/` is already covered by the live `hermes-r2-daily-backup` cron job.
`hermes_r2_backup_cron.sh` is a lock/report wrapper that shells out to
`hermes_r2_backup.sh`, which archives the **whole** `~/.hermes` tree with a short
explicit exclude list:

```sh
tar -cf "$TARBALL_RAW" \
  --exclude ".hermes/hermes-agent" \
  --exclude ".hermes/state-snapshots" \
  --exclude ".hermes/lsp" \
  --exclude ".hermes/bin/uv" \
  --exclude ".hermes/bin/uvx" \
  --exclude ".hermes/models_dev_cache.json" \
  --exclude ".hermes/provider_models_cache.json" \
  --exclude ".hermes/ollama_cloud_models_cache.json" \
  --exclude ".hermes/context_length_cache.yaml" \
  --exclude ".hermes/cache" \
  --exclude ".hermes/state.db" \
  --exclude ".hermes/state.db-wal" \
  --exclude ".hermes/state.db-shm" \
  --exclude ".hermes/auth.json" \
  -C /Users/claysystemshq .hermes
```

`logs/` is not in that list, and the script's own manifest lists it as included:

```python
"included_top_level": [
    ...,  "hooks/", "image_cache/", "images/", "kanban/", "kanban.db", "logs/",
```

There is no size filter anywhere in the script. `write_audit/` therefore rides
along in the daily and monthly R2 archives from its first write. No second backup
mechanism was built for it.

## Microsoft Graph — `tools/msgraph_write_client.py`

No wrapper existed for Graph contact/task *writes*; the pre-existing
`tools/microsoft_graph_client.py` is a generic async REST helper with no audit
gate. These clients are the supported way to mutate Graph contacts and tasks.

```python
from tools.msgraph_write_client import (
    MicrosoftGraphContactsWriteClient,
    delegated_token_provider,
)

contacts = MicrosoftGraphContactsWriteClient(delegated_token_provider("clayduncan"))
contacts.update_contact(
    "AAMkAD01",
    {"companyName": "Acme"},
    trigger="Nathan intro triage 2026-08-21",
)
```

Endpoints wrapped:

- `POST/PATCH/DELETE https://graph.microsoft.com/v1.0/me/contacts[/{id}]`
- `POST/PATCH/DELETE https://graph.microsoft.com/v1.0/me/todo/lists/{listId}/tasks[/{id}]`

Requires a delegated token with `Contacts.ReadWrite` / `Tasks.ReadWrite` — see
`scripts/microsoft_graph_delegated_setup.py`.

## GoHighLevel — `tools/ghl_client.py`, and a required behaviour change

GHL writes used to happen only through MCP tool calls
(`mcp__ghl_chillcabins__contacts_upsert_contact` and the equivalents for
`clay_personal`, `team_duncan`, `tracey`). An MCP tool call is dispatched by the
harness to a separate server process — it never crosses this repo's process
boundary, so there is no function to wrap and no pre/post hook this repo can
install on it. Even if one existed, it would be unenforceable: any caller could
still invoke the tool directly and bypass the log, which is exactly the failure
mode the log exists to rule out.

So `tools/ghl_client.py` performs the same writes over direct REST against
`services.leadconnectorhq.com`, using the same Private Integration Token the MCP
server uses (`MCP_GHL_<ACCOUNT>_API_KEY` in `~/.hermes/.env`), with the audit gate
built into the write path.

**Behaviour change, not a nice-to-have: GHL writes go through this client. Stop
calling the GHL MCP write tools directly.** The MCP tools remain the right choice
for *reads* and for endpoints this client does not cover — reads mutate nothing
and need no audit line.

| MCP write tool | replacement |
| --- | --- |
| `mcp__ghl_*__contacts_upsert_contact` | `GoHighLevelWriteClient.upsert_contact` |
| `mcp__ghl_*__contacts_create_contact` | `GoHighLevelWriteClient.create_contact` |
| `mcp__ghl_*__contacts_update_contact` | `GoHighLevelWriteClient.update_contact` |
| tag add / remove | `add_tags` / `remove_tags` |
| (no MCP equivalent) | `delete_contact` |

```python
from tools.ghl_client import GoHighLevelWriteClient

ghl = GoHighLevelWriteClient("team_duncan", location_id="…")
ghl.upsert_contact(
    {"email": "nathan@example.com", "firstName": "Nathan"},
    trigger="Nathan intro triage 2026-08-21",
)
```

`upsert_contact` resolves the live record first (by `id`, else by
`/contacts/search/duplicate` on email/phone). That pre-write lookup is what keeps
the log honest — it decides whether the entry is a `create` or an `update` and
supplies `before` for the update case, instead of logging every upsert as a
create. An upsert with no `id`, `email`, or `phone` is refused before the gate,
because the audit line could not be truthful.

Tag writes are logged as `update` operations on the contact, with the contact's
full tag array in `before` and `after`.

## Query tool

```
python scripts/write_audit_query.py --record-id AAMkAD01
python scripts/write_audit_query.py --destination ghl_contacts
python scripts/write_audit_query.py --destination ghl_contacts --record-id g-1
python scripts/write_audit_query.py --after-fetch-failed
python scripts/write_audit_query.py --log-dir /path/to/write_audit
```

Filters combine with AND. Matches are printed as pretty JSON, one object per
match, in chronological order across all month files. Unparseable lines are
skipped so one corrupt line cannot hide the rest of the history. A `--record-id`
value never matches an entry whose `record_id` is `null` (a create's intent line).

## Tests

| file | covers |
| --- | --- |
| `tests/test_write_audit_log.py` | real-filesystem append, `0600`, one fsync per append, byte-for-byte immutability of existing lines, loud failures, structural guarantees |
| `tests/test_msgraph_write_client.py` | log-failure-blocks-the-write for all six Graph operations, before/after correctness, read-back failure |
| `tests/test_ghl_client.py` | the same for all seven GHL operations |
| `tests/test_write_audit_query.py` | the CLI as a subprocess against two months of real on-disk entries |

The load-bearing assertion throughout is `transport.writes == []` when the append
fails. Checking the destination's final state cannot prove ordering — a mutation
that never happened and one that was rolled back look identical from outside. Only
counting invocations of the destination transport tells them apart.
