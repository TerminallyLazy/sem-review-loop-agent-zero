# Local validation

Run from the Agent Zero checkout root:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.venv/lib/python3.12/site-packages \
/opt/homebrew/bin/python3.12 -m pytest \
  usr/plugins/sem_review_loop/tests -q -p no:cacheprovider
```

The suite is self-contained and skips the real-binary check unless
`SEM_TEST_BINARY` is set. The local pinned macOS arm64 binary was validated
with:

```bash
SEM_TEST_BINARY=usr/plugins/sem_review_loop/.data/bin/darwin-arm64/sem \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.venv/lib/python3.12/site-packages \
/opt/homebrew/bin/python3.12 -m pytest \
  usr/plugins/sem_review_loop/tests/test_real_sem.py -q -p no:cacheprovider
```

That integration check creates a temporary Git worktree, verifies working and
staged entity diffs, then exercises bounded `sem_context` and `sem_impact`
queries. A separate local smoke check starts the plugin launcher and completes
the MCP `initialize` handshake with `SEM_NO_NETWORK=1` and telemetry disabled.

The managed install is reversible: it stores only under the plugin-owned
`.data/` directory. Uninstall preserves a drifted project MCP entry instead of
overwriting user changes.

## Observed local results (2026-07-31, macOS arm64)

- Plugin suite: `601 passed, 1 skipped` (the skip is the opt-in real-binary
  test when `SEM_TEST_BINARY` is unset).
- Plugin suite with the pinned binary enabled: `601 passed`.
- Pinned integration: `1 passed`, including working/staged diffs and native
  context/impact queries.
- Pinned executable: `sem 0.21.0`, SHA-256
  `818c7af64e71b71c37dee84ad5096b05ea09c9b0401828f818c685d3da13b81d`.
- Framework regressions: WebUI extension surfaces `45 passed`, component
  loader `1 passed`, projects `10 passed`, MCP handler `14 passed`.
- `compileall`, JavaScript syntax checks, and the Agent Zero plugin validator
  all passed.
- The plugin launcher completed a native MCP `initialize` handshake with the
  closed local environment and exited cleanly after termination.
- Agent Zero served the compatibility worktree on `127.0.0.1:50124`:
  `/api/health`, `panel.html`, and `sem-review-store.js` each returned HTTP
  200; the server was stopped and the port was confirmed closed.
- Browser acceptance used the synthetic `SEM Review Visual Check` project:
  the right-side drawer rendered `Project root`, one structural `authorize`
  change, a review card, wrapped context output, and red/green semantic diff
  lines. Selecting the change opened the Review tab.
- Project MCP acceptance used the same project: Preview -> Confirm -> Enable
  changed the drawer to `MCP armed`; the native stdio server returned the
  `2024-11-05` initialize handshake, `sem_diff` returned one modified entity,
  and focused `sem_context`/`sem_impact` calls returned local results. The
  managed entry carried `SEM_NO_NETWORK=1`, `SEM_NO_TELEMETRY=1`, and disabled
  `sem_entities`, `sem_blame`, and `sem_log`. Disable returned the drawer to
  `MCP off` and removed the managed project entry.
- No model-backed Agent Zero coding task was run in this environment because
  the selected model had no API key; the local result proves the review/MCP
  loop, not an empirical improvement benchmark for coding quality.
