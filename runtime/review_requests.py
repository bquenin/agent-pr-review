#!/usr/bin/env python3
"""Start T3 reviews automatically when someone requests your review.

Cron runs --poll every minute. Only direct review requests made after a host's
first poll (or after switching its gh account) start a review; a later
re-request starts another one. Launches use the installed agent-pr-review
launcher, so the configured posting policy applies.
"""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import review_config


QUERY = "is:pr is:open draft:false archived:false user-review-requested:@me"
LAUNCH_TIMEOUT_SECONDS = 15 * 60
MAX_ATTEMPTS = 3
RETRY_DELAY = datetime.timedelta(minutes=5)  # Multiplied by the attempts so far.
# Timelines are refetched when the PR changes or reappears, and at least this
# often, so pending requests do not cost one API call per PR per minute.
RECHECK_INTERVAL = datetime.timedelta(minutes=10)
PRUNE_AFTER = datetime.timedelta(days=30)
LOG_LIMIT_BYTES = 1 << 20
OUTPUT_LINES = 40
ERROR_LINES = 5
STATUS_LIMIT = 20
ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(stamp):
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def later(stamp, than):
    return bool(stamp) and (not than or parse(stamp) > parse(than))


def resource_dir():
    return Path(os.environ.get("AGENT_PR_REVIEW_RESOURCES",
        str(Path.home() / ".local/share/agent-pr-review"))).expanduser().resolve()


def directory():
    return resource_dir() / "review-requests"


def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save(path, state):
    # A poll killed mid-write must leave either the previous state or the new one.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        try:
            json.dump(state, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
            os.replace(out.name, path)
        finally:
            if os.path.exists(out.name):
                os.unlink(out.name)


def log(root, message, output=""):
    path = root / "poll.log"
    if path.exists() and path.stat().st_size > LOG_LIMIT_BYTES:
        os.replace(path, root / "poll.log.1")
    with path.open("a") as out:
        out.write(f"{now()} {message}\n")
        for line in output.splitlines()[-OUTPUT_LINES:]:
            out.write(f"    {line}\n")


def gh(host, endpoint, *options):
    binary = shutil.which("gh")
    if not binary:
        raise RuntimeError("gh is not on PATH; rerun --install-cron from a shell where it is")
    result = subprocess.run([binary, "api", "--hostname", host, endpoint, *options],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
    if result.returncode:
        # gh reports the HTTP status on stderr; response bodies stay out of the log.
        detail = (result.stderr.strip().splitlines() or [""])[-1][:200]
        raise RuntimeError(f"gh api {endpoint.split('?')[0]} failed (exit {result.returncode}) {detail}".rstrip())
    return json.loads(result.stdout)


def search(host):
    """Open, non-draft PRs that request the user's review directly, and whether GitHub returned all of them."""
    pages = gh(host, "search/issues", "-X", "GET", "-f", "q=" + QUERY, "-f", "per_page=100", "--paginate", "--slurp")
    return [item for page in pages for item in page["items"]], not any(page.get("incomplete_results") for page in pages)


def identify(host, item, config):
    # Validate before the URL reaches the launcher.
    pr_host, owner, repo, number = review_config.parse_pr(item["html_url"], config)
    if pr_host != host:
        raise ValueError(f"search on {host} returned {item['html_url']}")
    return f"{host}/{owner}/{repo}/pull/{number}", f"https://{host}/{owner}/{repo}/pull/{number}"


def requested_at(key, login):
    """Latest time the user was requested directly; team requests are excluded by design."""
    host, owner, repo, _, number = key.split("/")
    pages = gh(host, f"repos/{owner}/{repo}/issues/{number}/timeline?per_page=100", "--paginate", "--slurp")
    stamps = [event["created_at"] for page in pages for event in page
        if event.get("event") == "review_requested" and event.get("created_at")
        and ((event.get("requested_reviewer") or {}).get("login") or "").lower() == login.lower()]
    return max(stamps, key=parse, default=None)


def launch(url):
    """Run the installed launcher once; return (succeeded, combined output, failure reason)."""
    launcher = shutil.which("agent-pr-review") or str(Path.home() / ".local/bin/agent-pr-review")
    try:
        # --rereview: a request after a finished review must start another pass.
        result = review_config.run_with_timeout([launcher, "--cli", "t3code", "--rereview", url], LAUNCH_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=Path.home())
    except subprocess.TimeoutExpired as error:
        return False, error.output or "", f"timed out after {LAUNCH_TIMEOUT_SECONDS // 60} minutes"
    except OSError as error:
        return False, "", str(error)
    return result.returncode == 0, result.stdout, f"exit {result.returncode}"


def launch_action(output):
    """What T3 did (started, resumed, rereviewed, already-running...), from the launcher's final JSON line."""
    for line in reversed(output.splitlines()):
        try:
            result = json.loads(line)
        except ValueError:
            continue
        if isinstance(result, dict) and isinstance(result.get("action"), str):
            return result["action"]
    return None


def consider(root, state, key, url, updated, cutoff, login, stamp, persist):
    reviews = state["reviews"]
    entry = reviews.get(key)
    if (entry is None or entry.get("seenAt") != state.get("lastPollAt") or entry.get("updatedAt") != updated
            or parse(stamp) - parse(entry["checkedAt"]) >= RECHECK_INTERVAL):
        requested = requested_at(key, login)
        if entry is None or later(requested, entry.get("requestedAt")):
            entry = reviews[key] = {"url": url, "requestedAt": requested, "attempts": 0,
                "lastAttemptAt": None, "result": None, "lastError": None}
            if not later(requested, cutoff):
                entry["result"] = "skipped"
                log(root, f"skipped {url}: " + ("requested before automatic reviews started"
                    if requested else "no direct review request in its timeline"))
        entry.update(updatedAt=updated, checkedAt=stamp)
    entry["seenAt"] = stamp
    if entry["result"] in ("launched", "skipped") or entry["attempts"] >= MAX_ATTEMPTS:
        return
    if entry["lastAttemptAt"] and parse(stamp) < parse(entry["lastAttemptAt"]) + RETRY_DELAY * entry["attempts"]:
        return
    # Persist the attempt first: a poll killed mid-launch counts as a failure.
    entry.update(attempts=entry["attempts"] + 1, lastAttemptAt=stamp, result="running")
    persist()
    succeeded, output, reason = launch(url)
    attempt = f"attempt {entry['attempts']}/{MAX_ATTEMPTS}"
    if succeeded:
        entry.update(result="launched", lastError=None)
        action = launch_action(output)
        log(root, f"launched {url} (requested {entry['requestedAt']}, {attempt}"
            + (f", T3 {action})" if action else ")"), output)
    else:
        lines = [line for line in output.splitlines() if line.strip()]
        entry.update(result="failed", lastError="\n".join(lines[-ERROR_LINES:]) or reason)
        final = "; giving up until a newer request" if entry["attempts"] >= MAX_ATTEMPTS else ""
        log(root, f"failed {url} ({attempt}, {reason}){final}", output)
    persist()


def poll(root, config_file=None, stamp=None):
    """Run one poll unless another is still running; return whether it ran."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "poll.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False  # The previous poll, or one of its launches, is still running.
        poll_locked(root, config_file, stamp or now())
        return True


def poll_locked(root, config_file, stamp):
    path = root / "state.json"
    state = read(path)
    errors = {}
    try:
        config = review_config.load(config_file)
    except ERRORS as error:
        config = None
        errors["config"] = str(error)
    if config is not None and not config["auto_review_hosts"]:
        return
    state.setdefault("startedAt", stamp)
    state.setdefault("hosts", {})
    state.setdefault("reviews", {})
    returned = set()
    searched = set()  # Hosts whose complete search lets us prune their unreturned records.
    for host in config["auto_review_hosts"] if config else []:
        # Each host has its own go-live cutoff, so adding a host later does not
        # review everything requested there since the first poll.
        host_state = state["hosts"].setdefault(host, {"startedAt": stamp})
        try:
            # Resolved every poll: gh auth switch changes whose requests we search.
            login = gh(host, "user")["login"]
            if host_state.get("login") and host_state["login"].lower() != login.lower():
                # The new account goes live now, like a newly added host. Its
                # records are dropped: they hold the old account's requests and
                # would hide or misdate the new account's.
                log(root, f"account on {host} changed from {host_state['login']} to {login}; "
                    "requests made before now are not reviewed automatically")
                host_state["startedAt"] = stamp
                for key in [key for key in state["reviews"] if key.startswith(host + "/")]:
                    del state["reviews"][key]
            host_state["login"] = login
            items, complete = search(host)
        except ERRORS as error:
            errors[host] = str(error)
            continue
        if complete:
            searched.add(host)
        else:
            # GitHub timed out part of the search; review what it found and retry the rest next poll.
            errors[host] = "search returned incomplete results"
        for item in items:
            key = f"{host} search result"
            try:
                key, url = identify(host, item, config)
                returned.add(key)
                consider(root, state, key, url, item.get("updated_at"), host_state["startedAt"],
                    host_state["login"], stamp, lambda: save(path, state))
            except ERRORS as error:
                errors[key] = str(error)
    for key, entry in list(state["reviews"].items()):
        # A disabled or failing host's records stay until its search succeeds again.
        if (key.split("/")[0] in searched and key not in returned
                and parse(stamp) - parse(entry["seenAt"]) > PRUNE_AFTER):
            del state["reviews"][key]
    previous = state.get("errors") or {}
    for source, message in errors.items():
        if previous.get(source) != message:  # Log persistent failures once.
            log(root, f"error {source}: {message}")
    state.update(lastPollAt=stamp, errors=errors,
        lastError="; ".join(f"{source}: {message}" for source, message in errors.items()) or None)
    save(path, state)


def status(root):
    state = read(root / "state.json")
    print(json.dumps({key: state.get(key) for key in ("startedAt", "lastPollAt", "lastError", "hosts")}))
    reviews = sorted(state.get("reviews", {}).items(),
        key=lambda item: item[1].get("lastAttemptAt") or item[1].get("checkedAt") or "", reverse=True)
    for key, entry in reviews[:STATUS_LIMIT]:
        print(json.dumps({"pr": key, **{field: entry.get(field) for field in
            ("result", "requestedAt", "lastAttemptAt", "attempts", "lastError")}}))


def cron_lines():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
    if result.returncode and "no crontab" not in result.stderr.lower():
        raise RuntimeError("Cannot read crontab; existing jobs left unchanged")
    return result.stdout.splitlines()


def cron_marker(resources):
    # Cron turns an unescaped % anywhere in the command, comments included, into a newline.
    return ("# agent-pr-review review requests " + str(resources)).replace("%", r"\%")


def install_cron(resources):
    # Cron's PATH lacks ~/.local/bin, gh and node; keep the installing shell's.
    marker = cron_marker(resources)
    lines = [line for line in cron_lines() if not line.endswith(marker)]
    command = shlex.join(["env", "PATH=" + os.environ.get("PATH", os.defpath),
        f"AGENT_PR_REVIEW_RESOURCES={resources}", sys.executable, str(Path(__file__).resolve()), "--poll"])
    command = command.replace("%", r"\%")
    lines.append(f"* * * * * {command} >/dev/null 2>&1 {marker}")
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True, timeout=30)


def uninstall_cron(resources):
    marker = cron_marker(resources)
    current = cron_lines()
    lines = [line for line in current if not line.endswith(marker)]
    if lines != current:
        subprocess.run(["crontab", "-"], input="".join(line + "\n" for line in lines), text=True, check=True,
            timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--poll", action="store_true", help="check for new review requests once (run by cron)")
    actions.add_argument("--status", action="store_true", help="show the last poll and recent automatic reviews")
    actions.add_argument("--install-cron", action="store_true", help="poll every minute from the user crontab")
    actions.add_argument("--uninstall-cron", action="store_true", help="remove the polling crontab line")
    args = parser.parse_args()
    if args.poll:
        poll(directory())
    elif args.status:
        status(directory())
    elif args.install_cron:
        if not review_config.load()["auto_review_hosts"]:
            raise ValueError("auto_review_hosts is empty; there is nothing to poll")
        install_cron(resource_dir())
    else:
        uninstall_cron(resource_dir())


if __name__ == "__main__":
    try:
        main()
    except ERRORS as error:
        print(f"agent-pr-review: {error}", file=sys.stderr)
        sys.exit(1)
