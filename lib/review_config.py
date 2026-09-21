"""Shared, data-only configuration and GitHub URL/repository validation."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit


DEFAULTS = {
    "github_hosts": ["github.com"],
    "repo_roots": ["~/code"],
    "transport": "ssh",
    "ssh_host": "",
    "devcontainer_workspace": "",
    "devcontainer_command": "devcontainer",
    "tmux_control_mode": False,
    "default_cli": "agent",
    "claude_model": "", "claude_effort": "",
    "cursor_model": "", "t3_model": "", "t3_effort": "",
    "node_extra_ca_certs": "",
    "host_instructions": {},
    "remote_host_aliases": {},
    "legacy_host": "",
    "trust_worktrees": False,
    "posting_policy": "review-only",
    "monitor": False,
}
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")
COMPONENT = r"[A-Za-z0-9_.-]+"
PR_PATH = re.compile(rf"/({COMPONENT})/({COMPONENT})/pull/([1-9][0-9]*)(?:/(?:files|commits|checks))?/?")


def config_path():
    return Path.home() / ".config/agent-pr-review/config.json"


def load(config_file=None):
    path = Path(config_file) if config_file else config_path()
    try:
        values = json.loads(path.read_text())
    except FileNotFoundError:
        values = {}
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys():
        raise ValueError(f"{path}: expected an object with documented configuration keys")
    config = {**DEFAULTS, **values}
    for key, default in DEFAULTS.items():
        if type(config[key]) is not type(default):
            raise ValueError(f"{path}: invalid type for {key}")
    for key in ("github_hosts", "repo_roots"):
        if not config[key] or not all(isinstance(item, str) and item.strip() for item in config[key]):
            raise ValueError(f"{key} must be a nonempty list of strings")
    config["github_hosts"] = list(dict.fromkeys(h.lower() for h in config["github_hosts"]))
    if not all(HOST.fullmatch(h) for h in config["github_hosts"]):
        raise ValueError("github_hosts must contain exact hostnames without schemes, ports or wildcards")
    if config["legacy_host"]:
        config["legacy_host"] = config["legacy_host"].lower()
        if config["legacy_host"] not in config["github_hosts"]:
            raise ValueError("legacy_host must be one of github_hosts")
    if "ssh_host" not in values:
        legacy = path.parent / "ssh-host"
        if legacy.exists():
            config["ssh_host"] = next((line.strip() for line in legacy.read_text().splitlines() if line.strip()), "")
    if config["ssh_host"] and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", config["ssh_host"]):
        raise ValueError("ssh_host must be an SSH alias or hostname")
    if config["transport"] not in ("ssh", "devcontainer", "local"):
        raise ValueError("transport must be ssh, devcontainer or local")
    workspace = config["devcontainer_workspace"]
    if workspace and not Path(workspace).expanduser().is_absolute():
        raise ValueError("devcontainer_workspace must be an absolute path or start with ~/")
    command = config["devcontainer_command"]
    if not command or command.startswith("-") or ("/" in command and not Path(command).expanduser().is_absolute()):
        raise ValueError("devcontainer_command must be an executable name or absolute path")
    if config["default_cli"] not in ("agent", "claude", "t3code"):
        raise ValueError("default_cli must be agent, claude or t3code")
    if config["posting_policy"] not in ("review-only", "comment", "approve"):
        raise ValueError("posting_policy must be review-only, comment or approve")
    if config["monitor"] and config["posting_policy"] == "review-only":
        raise ValueError("monitor requires comment or approve posting_policy")
    for host, instruction in config["host_instructions"].items():
        if host not in config["github_hosts"] or not isinstance(instruction, str) or "\0" in instruction:
            raise ValueError("host_instructions must map configured hosts to instruction text")
    for alias, host in config["remote_host_aliases"].items():
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", alias) or host not in config["github_hosts"]:
            raise ValueError("remote_host_aliases must map SSH aliases to configured GitHub hosts")
    for key, value in config.items():
        if isinstance(value, str) and any(ord(c) < 32 for c in value):
            raise ValueError(f"{key} cannot contain control characters")
    for root in config["repo_roots"]:
        if not Path(root).expanduser().is_absolute() or any(ord(c) < 32 for c in root):
            raise ValueError("repo_roots must be absolute paths or start with ~/")
    return config


def parse_pr(value, config=None):
    if not isinstance(value, str) or any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("Expected an HTTPS GitHub PR URL")
    url = urlsplit(value)
    if url.scheme not in ("https", "agent-pr-review") or url.username or url.password or url.port:
        raise ValueError("Expected HTTPS without credentials or a port")
    host = url.hostname or ""
    if not HOST.fullmatch(host) or url.netloc.lower() != host:
        raise ValueError("Invalid GitHub hostname")
    if config is not None and host not in config["github_hosts"]:
        raise ValueError(f"Host {host} is not in github_hosts in {config_path()}")
    match = PR_PATH.fullmatch(url.path)
    if not match:
        raise ValueError("Expected /owner/repository/pull/positive-number")
    owner, repo, number = match.groups()
    if owner in (".", "..") or repo in (".", ".."):
        raise ValueError("Invalid owner or repository")
    # GitHub repository/owner names are case insensitive; normalize identities.
    return host, owner.lower(), repo.lower(), number


def remote_identity(value, aliases):
    if "://" not in value:
        match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", value)
        if not match:
            return None
        host, repo_path = match.groups()
    else:
        url = urlsplit(value)
        if url.scheme not in ("https", "http", "ssh", "git"):
            return None
        host, repo_path = url.hostname or "", url.path.lstrip("/")
    host = aliases.get(host, host).lower()
    repo_path = repo_path.removesuffix(".git")
    if not re.fullmatch(rf"{COMPONENT}/{COMPONENT}", repo_path) or any(part in (".", "..") for part in repo_path.split("/")):
        return None
    return f"{host}/{repo_path}".lower()


def matching_remote(directory, expected, aliases):
    # Read configured URLs before Git's insteadOf transport rewrites are applied.
    result = subprocess.run(["git", "-C", str(directory), "config", "--get-regexp", r"^remote\..*\.url$"], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        result.check_returncode()
    matches, seen = [], set()
    for line in result.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            continue
        key, value = fields
        name = key[len("remote."):-len(".url")]
        if name in seen:
            continue
        seen.add(name)  # Git fetch uses the first configured URL.
        if remote_identity(value, aliases) == expected:
            matches.append(name)
    if not matches:
        raise ValueError(f"No fetch remote in {directory} matches {expected}")
    return "origin" if "origin" in matches else sorted(matches)[0]


def select_repo(config, expected, explicit=None):
    if explicit:
        directory = Path(explicit).expanduser().resolve()
        matching_remote(directory, expected, config["remote_host_aliases"])
        return directory
    candidates = set()
    for root in config["repo_roots"]:
        root = Path(root).expanduser().resolve()
        for current, dirs, files in os.walk(root):
            relative = Path(current).relative_to(root)
            if ".git" in dirs or ".git" in files:
                try:
                    matching_remote(current, expected, config["remote_host_aliases"])
                    candidates.add(Path(current).resolve())
                except (ValueError, subprocess.CalledProcessError):
                    pass
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if d not in (".git", ".agent-pr-review", ".venv", "node_modules")] if len(relative.parts) < 4 else []
    if len(candidates) != 1:
        detail = ", ".join(str(p) for p in sorted(candidates)) or "none"
        raise ValueError(f"Expected one clone of {expected}; found {detail}. Use --repo PATH or configure repo_roots.")
    return candidates.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "get", "url", "repo", "remote"))
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    config = load()
    if args.action == "get":
        value = config[args.values[0]]
        if isinstance(value, dict):
            value = value.get(args.values[1], "")
        print(str(value).lower() if isinstance(value, bool) else value)
    elif args.action == "url":
        parts = parse_pr(args.values[0], config)
        cli = parse_qs(urlsplit(args.values[0]).query).get("cli", [""])[0]
        if cli and cli not in ("agent", "claude", "t3code"):
            raise ValueError("Unknown cli in PR URL")
        print("\t".join((*parts, cli)))
    elif args.action == "repo":
        print(select_repo(config, args.values[0], args.values[1] if len(args.values) > 1 else None))
    elif args.action == "remote":
        print(matching_remote(args.values[0], args.values[1], config["remote_host_aliases"]))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"agent-pr-review: {error}", file=sys.stderr)
        sys.exit(1)
