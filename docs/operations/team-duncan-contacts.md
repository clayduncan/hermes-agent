# Team Duncan Contact Activation Registry: Operational Reference

Plugin: `team_duncan_contacts`
Build: OPS-72
Status: Isolated build, not yet enabled in the live profile.
Prerequisite: OPS-16 (contact creation in GHL) must be satisfied before activation.

---

## Purpose

Provides a Clay-approved, two-step contact activation flow for the Team Duncan
GoHighLevel account. Activation tracking begins only at the moment Clay confirms
each contact. Activity before that timestamp is unavailable through the registry.
Phone handle resolution (OPS-46) is absorbed inside the registry so raw handles
never cross into agent-visible inputs, outputs, logs, or stored state.

---

## Paths

| Resource | Path |
|---|---|
| Plugin code | `plugins/team_duncan_contacts/` (in git) |
| Runtime state | `$HERMES_HOME/plugin-data/team_duncan_contacts/registry.json` |
| HMAC key | `$HERMES_HOME/plugin-data/team_duncan_contacts/hmac_key` |
| **Activity ledger** | `$HERMES_HOME/plugin-data/team_duncan_contacts/activity.db` |
| Plugin data root | `$HERMES_HOME/plugin-data/team_duncan_contacts/` |

Runtime PII and registry state belong outside git. The plugin code and tests
belong in git. Never commit `registry.json`, `hmac_key`, or `activity.db`.

---

## Invariants

1. `activated_at` is written exactly once per contact at the confirmation clock
   tick. No subsequent operation may modify or backdate it.
2. Transition history is append-only. Pause, resume, and retire append records;
   they never remove existing entries.
3. Raw phone numbers and email addresses are never stored in `registry.json`.
   Matching uses keyed HMAC of the canonical handle.
4. The HMAC key file (`hmac_key`) is binary, 32 bytes, mode `0600`. It is never
   logged, returned, or serialized.
5. `registry.json` is written atomically via a temp file + `os.replace()` with
   an intermediate `fsync`. No partial write is observable.
6. If contacts exist in the state file and the HMAC key is missing, the plugin
   refuses to start (fail closed).
7. Retired contacts cannot be reactivated. This build has no retirement-reversal
   path; a future Clay-approved rule must add it explicitly.

---

## Lifecycle States

```
  ┌──────────┐  confirm_activation   ┌────────┐
  │ (pending)│ ─────────────────────►│ active │
  └──────────┘                       └────────┘
                                        │    ▲
                               pause   │    │ resume
                                        ▼    │
                                     ┌────────┐
                                     │ paused │
                                     └────────┘
                                        │
                               retire  │
                                        ▼
                                     ┌─────────┐
                                     │ retired │  (terminal)
                                     └─────────┘
```

- **active**: events at or after `activated_at` are authorized.
- **paused**: new events are denied; history and cutoff are preserved.
- **retired**: all new events denied; not reversible in this build.

---

## Backup Expectations

Runtime state under `$HERMES_HOME/plugin-data/team_duncan_contacts/` is
covered by the existing Hermes-state backup system. No new backup job is
added in this build. Backup the data directory, including:

- `registry.json`: contacts, HMAC indexes, history, lifecycle state
- `hmac_key`: required to resolve events after restore
- `activity.db`: the contact activity ledger (WAL mode; covered by `hermes backup`)

A restore without the `hmac_key` file leaves existing contacts unresolvable.
The key must be restored alongside the state file with mode `0600`.

**Do not add a new backup job.** The existing backup system covers this path.

### Activity Ledger Online Backup (OPS-74)

```python
from plugins.team_duncan_contacts import activity_ledger
activity_ledger.backup(Path("/path/to/backup.db"))
```

`backup()` uses SQLite online backup (WAL-safe, works with open connections).
`dest_path` must not already exist. The live ledger is never cleared or replaced
in place by any API method. To restore: validate a backup copy in a throwaway
`ActivityLedger` instance, then replace the live file at the OS level after
stopping the agent.

---

## Install and Reload Steps

These steps are outside the OPS-72 build loop and must be performed by Clay
manually after review is complete.

1. Verify `tests/plugins/team_duncan_contacts/test_registry.py` passes green.
2. Set `location_id` under `plugins.entries.team_duncan_contacts.settings` in
   `config.yaml` to the correct GoHighLevel location ID for Team Duncan.
3. Add `team_duncan_contacts` to `plugins.enabled` in `config.yaml`.
4. Reload Hermes (restart the agent process or use the plugin reload command).
5. On first startup, the registry will generate `hmac_key` automatically if
   no contacts are registered yet. Back up immediately after first use.

**Do not enable this plugin in the live profile before completing the above steps.**

---

## Agent Tool Surface

| Tool | Input | Accepts | Returns |
|---|---|---|---|
| `prepare_activation` | `name_or_id` | Contact name or GHL contact ID | Masked handles, contact ID, location ID, opaque token |
| `confirm_activation` | `token` | Opaque token from prepare | Activation record with timestamp |

Neither tool accepts phone numbers, email addresses, or Apple/iMessage handles.

---

## Source-Event Resolution (Internal API)

```python
registry.resolve_event(raw_handle: str, event_ts: datetime) -> ResolveResult
```

For OPS-46 absorption. Called inside the trusted collector/registry boundary.
Never exposed through an agent tool or CLI. Returns `decision` (one of:
`allow`, `deny_pre_activation`, `deny_paused`, `deny_retired`,
`review_required`) and masked metadata. Writes no state.

---

## Privacy Boundary

- Raw handles are visible only inside `ghl_reader.py` (prepare path) and
  `resolve_event()` (resolver path).
- `registry.json` stores only HMAC indexes and masked display labels.
- Agent-facing outputs are sanitized by `sanitizer.py` as defense-in-depth.
- No exception path, debug log, or error message serializes raw handles.

---

## Restrictions

- This build performs **no GHL writes**. A contact that does not exist in GHL
  returns `contact_creation_required` (OPS-16 prerequisite).
- Do not use GHL MCP write tools from activation or resolver paths.
- Do not modify core Hermes modules for this plugin.
