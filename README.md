# Semantic Review Loop

Semantic Review Loop adds entity-level code review to Agent Zero with
[Ataraxy Labs sem](https://github.com/Ataraxy-Labs/sem). It presents
structural and cosmetic changes in the **Semantic Review** surface and can
expose `sem_diff`, `sem_context`, and `sem_impact` through project MCP.

## Safety defaults

- Local-only semantic analysis with telemetry, update checks, cloud access,
  and unmanaged sidecars disabled
- Explicit **Enable MCP** approval for each Agent Zero project
- Bounded self-review; Automatic Repair is off by default and stops after the
  configured maximum repair cycles
- Lessons remain inactive until approved from a review card and stay scoped
  to the project that produced them
- No staging, commits, reverts, pushes, global package managers, shell-profile
  edits, Git configuration changes, or system services

Installing the pinned executable is the one supported network operation: the
install hook or manual Run action downloads an exact official GitHub release
asset over HTTPS. Review, repair, and lesson workflows do not send source code
or review data to sem cloud services.

## Install or repair sem

Install the plugin below Agent Zero's `usr/plugins/sem_review_loop` directory,
then use its **Run** action. The action downloads the pinned official sem
v0.21.0 asset for supported macOS, Linux, or Windows systems, verifies its
SHA-256 digest, validates the exact CLI identity and version, and installs it
below the plugin's ignored `.data/` directory. Both the archive and extracted
executable have independently pinned SHA-256 digests. Running the action again
verifies cached bytes before execution, reuses a valid installation, or
repairs it atomically. It does not use a global package manager.

A custom executable may be selected in project settings, but it must be an
existing regular executable that reports exactly `sem 0.21.0`. The plugin
never discovers or trusts an arbitrary `sem` from `PATH`.

On an unsupported platform, plugin installation succeeds without a managed
binary. Configure an absolute custom executable, then use **Run** or the first
semantic review to validate its identity and version.

## Use it

1. Enable the plugin for a project from the Agent Zero plugin Switch.
2. Open the **Semantic Review** right-canvas surface.
3. Select **Enable MCP**, inspect the exact local stdio entry, and confirm.
4. Edit code normally. The Changes tab refreshes from mutation events.
5. Before task completion, Agent Zero reviews uncheckpointed structural
   changes and records `pass`, `repaired`, or `unresolved`.
6. Approve only useful lesson cards in the Lessons tab.

Automatic Repair uses ordinary Agent Zero edits. It never stages, commits,
reverts, or pushes, and it cannot exceed the configured project repair bound.

## Remove it

Disable MCP first when practical, then uninstall the plugin. Uninstall removes
only exact plugin-managed MCP entries and plugin-owned binaries, caches,
pending proposals, receipts, and approved lessons. A drifted MCP entry is preserved
and reported for manual review. This reversible cleanup does not
remove project source, Git state, global tools, or services.

## Supported systems

- macOS arm64 and x86_64
- Linux arm64 and x86_64
- Windows x86_64

Other systems require a custom sem v0.21.0 executable. The bundled installer
does not support Windows arm64 or other architectures.

## Limitations

- Semantic Git comparisons require a Git worktree. Manual file review can
  degrade gracefully in a non-Git project, but commit and branch comparisons
  are unavailable there.
- Version 1 supports only sem v0.21.0.
- Project MCP is never enabled automatically.
- Automatic Repair is opt-in and bounded; unresolved findings remain visible
  for a person to decide.
- Approved lessons are project-local and are not a global memory collection.

## License

MIT. See `THIRD_PARTY_NOTICES.md` for sem licensing.
