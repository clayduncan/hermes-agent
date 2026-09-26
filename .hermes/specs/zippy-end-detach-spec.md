# Hermes Zippy Voice End Detach Specification

## Goal

Once an API/Zippy Voice `prompt.submit` is accepted and acknowledged, its agent run and all inline tool execution belong to the server-side session, not the lifetime of the submitting WebSocket or HTTP request. Closing the voice UI, pressing End, losing the network, or closing the client connection detaches the subscriber but does not cancel the accepted run.

## Contract

- An accepted prompt runs in a server-owned task/registry keyed to session and turn identity.
- WebSocket/request disconnect removes only delivery subscription and connection-owned resources.
- The accepted agent run continues through model calls, inline terminal/file/browser tools, and a `claude-build launch` tool call.
- Completion, assistant text, tool results, and final status persist to normal session history and are visible on later `session.resume`.
- If a client reconnects before completion, current events may resume without duplicating the run.
- The only user cancellation path is explicit `session.interrupt` with the exact active turn. It must still cancel promptly.
- Gateway shutdown may follow existing bounded drain semantics. This feature does not promise survival across process death.
- No synthetic completion notification or replay wake is generated merely because the client detached.
- Existing turn-ID isolation, failed-turn retention, prompt caching, role alternation, process completion safeguards, and multi-client behavior remain intact.

## Scope

Trace the real Zippy Voice API/TUI gateway submit, websocket disconnect, session cleanup, runner/task ownership, and cancellation paths. Fix the entire disconnect-cancels-run class, not only the iOS caller.