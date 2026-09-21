# Agent PR Review

Launch and resume AI-assisted pull request reviews in isolated Git worktrees.
Works with **GitHub.com, GitHub Enterprise Server (GHES), and GitHub Enterprise
Cloud (GHEC)** using the same installation, including multiple hosts at once.

Choose Cursor CLI, Claude Code, or a running T3 Code server. The optional Chrome
extension adds a review button to PR pages. Reviews can stay in the agent session,
post comments, or approve clean PRs; monitoring for new commits and feedback is
optional.

## Supported setup

- **CLI:** Linux development host with Bash 5+, Python 3.10+, Git, GitHub CLI,
  `jq`, and GNU `timeout`. Install and authenticate the backend you want to use.
  You do not need all three backends.
- **Browser launcher:** macOS with Chrome, Python 3.10+, Xcode command line tools,
  and SSH access to that Linux host. Cursor/Claude terminal launches use iTerm2;
  T3 launches run in the background without opening a terminal.
- **T3:** a running Linux T3 server with its authenticated orchestration HTTP API
  and `auth session` CLI. Runtime discovery uses Linux `/proc`. T3 integration is
  version-sensitive; `--check` below verifies connectivity. Windows and a local
  macOS review runtime are not currently supported.

The test suite covers configured public and enterprise host routing using local
fixtures. A new GHES version or T3 build should also get a live smoke test before
being treated as supported.

## Install

Clone this repository on each machine that runs a component:

```bash
git clone https://github.com/bquenin/agent-pr-review.git
cd agent-pr-review
```

Create `~/.config/agent-pr-review/config.json` on both the Mac and development
host. Use the same `github_hosts` list; paths and the SSH alias are local settings.
A minimal CLI-only install can omit this file and use GitHub.com with repositories
under `~/code`.

```json
{
  "github_hosts": ["github.com", "github.example.com", "octocorp.ghe.com"],
  "ssh_host": "dev-host",
  "repo_roots": ["~/code"],
  "default_cli": "agent",
  "posting_policy": "review-only",
  "monitor": false,
  "trust_worktrees": false
}
```

Replace the example enterprise hosts with your actual hosts, or omit them.
`github_hosts` contains exact names without schemes, ports or wildcards. GHEC
organizations on GitHub.com use `github.com`; enterprises with a dedicated
`*.ghe.com` domain configure that exact domain. See [GitHub's GHEC
hosting documentation](https://docs.github.com/en/enterprise-cloud@latest/admin/overview/about-github-enterprise-cloud).

On the **Linux development host**, authenticate GitHub CLI for each host and
install the runtime:

```bash
gh auth login --hostname github.com
gh auth login --hostname github.example.com
bash vm/install.sh
```

Git fetch credentials must also work for each clone's remote. The launcher uses
existing Git/GitHub CLI authentication; it does not store tokens in its config.
GitHub CLI's [authentication environment variables](https://cli.github.com/manual/gh_help_environment)
can override stored authentication, so use host-appropriate credentials when
setting them yourself. Ensure `~/.local/bin` is on `PATH`.

On the **Mac**, set `ssh_host` to a working alias from your SSH configuration and
install the browser bridge:

```bash
bash host/install.sh
```

This compiles `~/Applications/AgentPRReview.app`, registers `agent-pr-review://`,
installs the native status bridge, and builds the extension at
`~/.local/share/agent-pr-review/extension` with your configured hosts. In
`chrome://extensions`, enable developer mode and load that directory unpacked.
The extension runs only on [explicitly matched hosts](https://developer.chrome.com/docs/extensions/develop/concepts/match-patterns).

Changing `github_hosts` or `default_cli` on the Mac requires rerunning
`host/install.sh` and reloading the extension. The extension remembers the user's
last backend selection; a changed default applies when no selection is saved.
Keep the generated extension in the same directory: its unpacked Chrome ID and
native messaging registration depend on that path. Source files are never edited
by installation.

Each installer deploys only its own machine. Re-run the relevant installer after
source updates; installed runtime files are copies. T3 watcher supervision is
optional: `bash vm/install.sh --enable-supervision` adds a marked cron entry
without replacing other jobs. Existing saved watchers are restarted on install.

## Use

Open a PR and use the review button, or run on the development host:

```bash
agent-pr-review 'https://github.com/owner/repo/pull/42'
agent-pr-review --cli claude 'https://github.example.com/team/repo/pull/42'
agent-pr-review --cli agent --repo ~/code/my-clone 'https://github.com/owner/repo/pull/42'
agent-pr-review --no-tmux 'https://github.com/owner/repo/pull/42'
agent-pr-review --print-cmd 'https://github.com/owner/repo/pull/42'
```

`--print-cmd` prepares and fetches a real worktree and prints the assembled prompt
and command; it does not start an agent, create a chat, accept trust, or post a
review. It is a launch preview, not a read-only command.

Repository discovery searches configured roots up to four levels deep and matches
Git remotes, including clones with renamed directories. Ambiguous matches require
`--repo PATH`; that clone must still have a remote matching the requested PR.
Fetch failures, dirty worktrees and diverged history stop the launch. A failure
never falls back to the canonical checkout. Inspect a diverged worktree (for
example after a force push), preserve any local commits, and remove that worktree
and its review branch before retrying if it is safe to do so.

New worktrees, branch names, sessions and tmux windows distinguish the GitHub
host, owner, repository and PR number. Cursor and Claude have separate session
identities and share a worktree for the same PR. Avoid running both against the
same worktree simultaneously. Clean cached worktrees older than 60 days can be
removed during a later launch; their branches are retained.

If tmux is installed, terminal reviews run in the `agents` session by default and
survive SSH disconnects. Use `--no-tmux` to run directly, or
`--tmux-session reviews` to choose a session. Without tmux, keep the terminal open
while reviewing or monitoring.

## Configuration

`~/.config/agent-pr-review/config.json` is JSON data, not executable shell code.
Unknown keys and invalid types fail validation. Settings are read on each launch.

| Key | Default | Purpose |
| --- | --- | --- |
| `github_hosts` | `["github.com"]` | Hosts permitted by launchers and browser bridge |
| `repo_roots` | `["~/code"]` | Local clone search roots |
| `ssh_host` | empty | Mac-to-Linux SSH alias; required for browser launches |
| `default_cli` | `agent` | `agent`, `claude`, or `t3code` |
| `claude_model`, `claude_effort` | empty | Omit flags and use the CLI defaults unless configured |
| `cursor_model` | empty | Cursor CLI model override |
| `t3_model`, `t3_effort` | empty | Fallback model/effort when T3 has no configured selection |
| `posting_policy` | `review-only` | `review-only`, `comment`, or `approve` |
| `monitor` | `false` | Continue reviewing updates; requires `comment` or `approve` |
| `trust_worktrees` | `false` | Explicitly accept Claude/Cursor workspace trust |
| `node_extra_ca_certs` | empty | Optional CA file for Node-based agents; existing environment wins |
| `host_instructions` | `{}` | Additional local instructions keyed by exact GitHub hostname |
| `remote_host_aliases` | `{}` | Map Git SSH host aliases to their actual GitHub host |
| `legacy_host` | empty | Host to which old, hostless sessions and worktrees belong |

`AGENT_PR_REVIEW_RESOURCES` can relocate runtime resources on the Linux host;
use the same value for install and launch. The Mac native status bridge currently
uses the default remote resource directory. `AGENT_PR_REVIEW_TMUX_SESSION` changes
the default tmux session. `NODE_EXTRA_CA_CERTS` remains supported directly.

For example, an organization can preserve autonomous reviews and internal skill
instructions without maintaining a fork:

```json
{
  "github_hosts": ["github.com", "github.example.com"],
  "ssh_host": "work-dev",
  "repo_roots": ["~/code", "~/projects"],
  "default_cli": "t3code",
  "posting_policy": "approve",
  "monitor": true,
  "trust_worktrees": true,
  "host_instructions": {
    "github.example.com": "Use the organization's locally installed GitHub skill for this host."
  },
  "remote_host_aliases": {
    "work-git": "github.example.com"
  }
}
```

The public package does not require an organization's skills, certificates or
services. Keep those settings outside the repository. The same installation can
review personal PRs on GitHub.com and work PRs on enterprise hosts.

### Review policy

- `review-only`: show the review inside the agent session; do not post.
- `comment`: submit comment reviews without approving or requesting changes.
- `approve`: approve when verification is complete and no author action is needed;
  otherwise comment. Never self-approve or request changes; avoid duplicate reviews.

The review methodology and approval checks live in `prompts/`. Configured policy
is included in initial and resumed prompts. These are agent instructions; actual
permissions come from your agent and GitHub account. See [SECURITY.md](SECURITY.md).
Stop existing sessions before changing policy; configuration does not revoke a
running agent's actions.

With monitoring enabled, terminal sessions use `pr-monitor.sh`, while T3 uses a
detached Python watcher. Both follow PR commits, feedback and closure. Feedback
pagination covers busy PRs; polling consumes GitHub API quota. On merge/close,
the prompt asks for cleanup only when local work can be preserved.

### T3

T3 must be running on the Linux host. Check connectivity without launching a review:

```bash
python3 ~/.local/share/agent-pr-review/t3-review.py --check
```

The helper discovers the server under `~/.t3` (override with `T3CODE_HOME`), issues
a short-lived local API session, and revokes it after use. For custom runtime
layouts, `AGENT_PR_REVIEW_T3_BIN` can name an executable wrapper for the T3 CLI.

Existing `~/.config/agent-pr-review/t3.json` remains supported. It can specify
`projectId`, `modelSelection` and `runtimeMode`. Otherwise reviews use the matching
project and server defaults, then the configured `t3_model` fallback. If no model
is configured anywhere, the helper reports an actionable error. Existing threads
retain their T3 model/runtime settings. T3 permissions are controlled by T3;
`trust_worktrees` applies to the two terminal CLIs.

T3 reuses completed threads without sending duplicate prompts. Status appears on
the browser button. When monitoring is enabled, commits or feedback start another
turn after the current turn finishes. When disabled, launching the PR stops its
saved watcher. Inspect or stop watchers with:

```bash
python3 ~/.local/share/agent-pr-review/t3_monitor.py --status
python3 ~/.local/share/agent-pr-review/t3_monitor.py --stop '<thread-uuid>'
```

## Migrate an existing installation

Keep existing clone paths, installed state and T3 settings. Before reinstalling:

1. Add all your GitHub hosts to `github_hosts` on both machines.
2. Set `ssh_host`, or retain the old `~/.config/agent-pr-review/ssh-host` file.
   An explicit JSON value takes precedence. There is no implicit SSH destination.
3. Choose `default_cli`, model overrides and policy explicitly. To preserve the
   previous autonomous behavior, set `posting_policy` to `approve`, `monitor` to
   `true`, and enable `trust_worktrees` only if you want the old trust behavior.
4. Set `legacy_host` on the Linux host to the one GitHub host used by your old
   sessions. Old `pr-N` worktrees are retained for that host. Claude transcripts
   resume at their original path, and Cursor mappings are copied to host-qualified
   keys on a real launch. Other hosts get distinct state. If old sessions span
   multiple hosts, leave this unset and start fresh sessions; old files stay intact.
5. Reinstall both components, load the generated extension directory, and remove
   the old unpacked extension to avoid duplicate buttons. Reselect your backend
   if Chrome assigns the generated extension a new ID.

T3 thread identities already include the full PR URL and repository root. With
`legacy_host` set, keeping the old worktree path also preserves those threads.

## Development

```bash
bash tools/check.sh
```

Checks require Python, Node.js 20+, Bash, Git, `jq` and ShellCheck; macOS also
requires Swift. Tests use local repositories and simulated API responses without
GitHub credentials. CI runs on Linux and macOS. See [CONTRIBUTING.md](CONTRIBUTING.md).

```
extension/   Chrome content script and native status worker
host/        macOS URL handler, native bridge, installer
lib/         shared configuration, URL validation and repository discovery
vm/          Linux launcher, backend adapters and monitors
prompts/     shared review methodology and autonomous posting rules
tools/       extension build and local checks
```

## Troubleshooting and uninstall

Launch diagnostics appear in the terminal. Mac bridge logs are at
`~/Library/Application Support/AgentPRReview/agent-pr-review.log`. Check SSH,
`gh auth status --hostname <host>`, backend authentication and `--print-cmd`
output before changing permissions.

To uninstall, first stop running review sessions/watchers and remove this tool's
marked T3 monitor line from `crontab -e` if supervision was enabled. Then remove:

- Mac: `~/Applications/AgentPRReview.app`,
  `~/Library/Application Support/AgentPRReview`,
  `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.agent_pr_review.status.json`,
  and the unpacked extension from Chrome.
- Linux: `~/.local/bin/agent-pr-review` and `~/.local/share/agent-pr-review`.

Configuration is under `~/.config/agent-pr-review` on each machine. Preserve it
and any review worktrees/transcripts you still need. The launcher adds
`.agent-pr-review/` and `.agent-pr-review-fetched-at` to the global Git ignore file;
remove those entries if no longer needed. Cursor and Claude authentication and
shared agent state belong to those tools and are retained.

## License

[MIT](LICENSE).
