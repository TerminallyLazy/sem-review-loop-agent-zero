# Validation

Run the plugin suite from the Agent Zero framework runtime, with the pinned
binary available. For a Docker installation:

```bash
docker exec -w /a0 \
  -e PYTHONPATH=/a0/tmp/sem-review-test-deps \
  -e SEM_TEST_BINARY=/a0/usr/plugins/sem_review_loop/.data/bin/linux-arm64/sem \
  agent-zero /opt/venv-a0/bin/python -m pytest \
  usr/plugins/sem_review_loop/tests -q -p no:cacheprovider
```

The temporary test dependency directory must contain pytest and pytest-asyncio;
it is not part of the plugin installation. Select the binary path for the
runtime architecture. Without `SEM_TEST_BINARY`, the real-binary test is skipped.

## Verified on 2026-09-08

- Full plugin suite: **626 passed**, including the real sem 0.21.0 binary,
  in the Linux arm64 Agent Zero framework runtime (Python 3.12).
- JavaScript syntax check and `git diff --check` passed.
- Regression coverage includes automatic MCP installation and recovery,
  preservation of manually changed MCP entries, concurrent refresh scheduling,
  Alpine proxy-safe polling timers, working-snapshot lesson approval,
  exact-fingerprint checkpoint discovery, and terminal finding disclosure.
- Live acceptance used the explicitly supplied server at port 50080 and a
  separate synthetic Git project, **Semantic Review QA 0908**. Other projects
  and agent conversations were not modified for this test.
- The panel automatically connected the three focused MCP tools. A real agent
  used `sem_diff`, `sem_context`, and `sem_impact` to review pricing functions
  and recorded three unresolved findings against the current fingerprint and
  all five changed entities.
- The Changes tab showed real entity diffs, search filtering, context, and
  impact relationships. A file edit outside the UI appeared automatically.
  The Review tab displayed the recorded findings and connected MCP state.
  The layout remained readable at a 390-pixel viewport.
- After reloading only the QA completion hooks, a fresh fingerprint completed
  with two checkpoint calls (status, record), one final response, visible
  unresolved findings, and no acknowledgement exception.

## Acceptance boundaries

The live task was a read-only code review. Automatic source repair and coding
quality improvement were not benchmarked. Installer conflict handling,
reconnection, lesson approval, and completion failure paths have regression
coverage; they were not all exercised through the live browser.

When testing edits in an already-running development server, cached Python
extension classes can retain older helper imports. Validate the loaded hooks
as well as files on disk. A normal fresh installation loads the updated code;
coordinate any shared-server restart with its other users.
