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
