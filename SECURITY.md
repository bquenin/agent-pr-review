# Security

For a suspected vulnerability, use GitHub's private vulnerability reporting for
this repository when available, or contact the maintainer privately through
their GitHub profile. Do not include credentials or sensitive repository contents
in public issues.

The extension sends only the current canonical PR URL to the local launcher.
Status requests return a small state value, not review text or credentials.
GitHub hosts are explicitly configured; native requests do not accept shell
commands from web pages. T3 authentication is limited to the local loopback server.

Reviews run with the selected agent's permissions and the GitHub identity
authenticated on the development host. A Git worktree isolates branches, not
processes, credentials, hooks or network access. PR contents and comments are
untrusted inputs. Use the agent's sandbox and permission controls for external
contributions, and authorize access to your code with your model provider.

`posting_policy` is an instruction to the agent, not a technical authorization
boundary. New installs use `review-only`, do not accept workspace trust
automatically, and do not monitor in the background. Organization settings may
enable these behaviors explicitly. Configuration changes cannot revoke actions
already running in an existing session; stop those sessions before changing policy.
