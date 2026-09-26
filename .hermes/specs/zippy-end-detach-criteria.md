# Hermes Zippy Voice End Detach Acceptance Criteria

1. Test accepts prompt.submit, blocks an inline tool, disconnects client, releases tool, and proves run completes and persists.
2. Same proof covers a terminal/background-build launch tool call boundary without leaking or killing its subprocess.
3. Reconnect/session.resume after detached completion returns the assistant result exactly once.
4. Disconnect before prompt acceptance does not create a run.
5. Explicit session.interrupt with matching turn ID still cancels the server-owned task promptly.
6. Stale/wrong turn ID interrupt cannot cancel a newer detached run.
7. Two clients attached to one session: one disconnect does not cancel run or other subscriber.
8. No replay notification/model wake is created from detach itself.
9. Task registry cleans completed/cancelled entries and does not leak.
10. Existing prompt, auto-continue, failed-turn retention, Zippy resume, turn-ID, and process-completion suites pass.
11. Implementation is integrated and activated in the running backend, followed by a real accepted-run/disconnect/resume proof.