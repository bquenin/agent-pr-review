# Contributing

Open an issue describing the behavior you want to change, or send a focused pull
request with a reproduction and validation. Use generic hosts and synthetic data
in fixtures. Keep organization-specific instructions and credentials outside the
repository.

Open the supplied devcontainer for the Linux dependencies, or install them
locally. CI also builds that container and runs the suite inside it.

Run `bash tools/check.sh` before submitting a change. The tests use temporary Git
repositories and simulated GitHub/T3 responses; they do not require accounts,
agent subscriptions, an enterprise instance, or network access. On macOS the
check also typechecks the URL handler with the Xcode command line tools.

The Linux launcher owns repository selection, worktree preparation and agent
launches. Shared configuration is in `lib/review_config.py`. The Mac handler owns
the Mac UI; shared Python code validates URLs and dispatches through SSH or
the Dev Container CLI. Keep GitHub.com, GHES, and GHEC behavior
covered when changing any boundary. Host permissions must remain explicit.

New backends must honor the configured review policy, preserve worktree
isolation, and keep session identity distinct across GitHub hosts. Document
external CLI/API compatibility assumptions and any migration of existing state.

Contributions are licensed under the project's MIT license.
