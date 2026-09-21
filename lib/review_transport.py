"""Host-side dispatch to a runtime over SSH, the Dev Container CLI, or directly on this Mac."""
import json
import os
from pathlib import Path
import shlex
import shutil
from urllib.parse import parse_qs, urlsplit

from review_config import parse_pr


def restore_host_path():
    # Launch Services and Chrome do not inherit the installing terminal's PATH.
    saved = Path(__file__).with_name("host-path.json")
    if saved.exists():
        value = json.loads(saved.read_text())
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("Invalid installed PATH; rerun host/install.sh")
        os.environ["PATH"] = value


def review_request(url, config):
    parts = parse_pr(url, config)
    cli = parse_qs(urlsplit(url).query, keep_blank_values=True).get("cli", [config["default_cli"]])[0]
    if cli not in ("agent", "claude", "t3code"):
        raise ValueError("Unknown review backend")
    return "https://" + "/".join((*parts[:3], "pull", parts[3])), cli


def command(config, *, url=None, cli=None, status=False):
    """Return argv; only fixed script text is interpreted by a runtime shell."""
    if status:
        script = 'exec python3 "$HOME/.local/share/agent-pr-review/t3-review.py" --status'
        arguments = []
    else:
        url, selected = review_request(url, config)
        cli = cli or selected
        if cli not in ("agent", "claude", "t3code"):
            raise ValueError("Unknown review backend")
        script = 'exec "$HOME/.local/bin/agent-pr-review" "$@"'
        arguments = [url, "--cli", cli]
    runtime = ["/bin/sh", "-c", script, "agent-pr-review", *arguments]
    if config["transport"] == "local":
        # The runtime is installed on this machine; no remote shell is involved.
        return runtime
    if config["transport"] == "devcontainer":
        workspace = Path(config["devcontainer_workspace"]).expanduser()
        if not config["devcontainer_workspace"] or not workspace.is_dir():
            raise ValueError("Set devcontainer_workspace to the local folder containing your devcontainer configuration")
        executable = shutil.which(str(Path(config["devcontainer_command"]).expanduser()))
        if not executable:
            raise ValueError("Dev Container CLI not found; install @devcontainers/cli and rerun host/install.sh")
        # exec connects to an existing container. Status polling never creates one.
        return [executable, "exec", "--workspace-folder", str(workspace), "--", *runtime]
    if not config["ssh_host"]:
        raise ValueError("Set ssh_host for SSH transport, or configure transport: devcontainer")
    background = status or cli == "t3code"
    options = ["-T", "-o", "BatchMode=yes"] if background else ["-t"]
    return ["/usr/bin/ssh", *options, "-o", "ConnectTimeout=5" if status else "ConnectTimeout=15",
            "-o", "ClearAllForwardings=yes", "--", config["ssh_host"], shlex.join(runtime)]
