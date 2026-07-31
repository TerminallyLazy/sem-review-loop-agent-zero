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

- Plugin suite: `598 passed, 1 skipped` (the skip is the opt-in real-binary
  test when `SEM_TEST_BINARY` is unset).
- Plugin suite with the pinned binary enabled: `599 passed`.
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
  200; the server was stopped and the port was confirmed closed. This is an
  HTTP/static smoke check, not a visual browser acceptance run.
