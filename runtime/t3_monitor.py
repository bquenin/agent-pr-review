#!/usr/bin/env python3
"""Durable development host PR watchers which wake reviews through T3's native turn API."""
import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid


def read(path):
    return json.loads(path.read_text())


def save(path, state):
    # A killed watcher must leave either the previous cursor or the new one.
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


@contextlib.contextmanager
def locked(path, blocking=True):
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def directory():
    return Path(os.environ.get("AGENT_PR_REVIEW_RESOURCES",
        str(Path.home() / ".local/share/agent-pr-review"))) / "t3-monitors"


def register(root, base, payload, thread_id, since):
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / (str(uuid.UUID(thread_id)) + ".json")
    url = urllib.parse.urlsplit(payload["prUrl"])
    match = re.fullmatch(r"/([^/]+/[^/]+)/pull/([0-9]+)", url.path)
    if not match or not url.hostname or url.scheme != "https":
        raise ValueError("Invalid monitored PR URL")
    with locked(path.with_suffix(".lock")):
        if path.exists():
            state = read(path)
        else:
            state = {"threadId": thread_id, "prUrl": payload["prUrl"],
                "host": url.hostname, "repo": match[1], "number": int(match[2]),
                "baseDir": str(base), "since": since, "head": payload["headSha"],
                "worktreePath": payload["worktreePath"], "feedback": {}, "pending": {},
                "delivery": None, "closed": None}
        gh_path = shutil.which("gh")
        if not gh_path:
            raise RuntimeError("gh is required for automatic PR monitoring")
        state.update(enabled=True, stoppedReason=None, ghPath=gh_path,
            worktreePath=payload["worktreePath"], resumePrompt=payload.get("resumePrompt", ""))
        save(path, state)
    ensure(path)
    return path


def stop(root, thread_id):
    path = root / (str(uuid.UUID(thread_id)) + ".json")
    if not path.exists():
        return
    with locked(path.with_suffix(".lock")):
        state = read(path)
        state.update(enabled=False, stoppedReason="Monitoring disabled")
        save(path, state)


def stop_worktree(root, worktree_path):
    stopped = 0
    for path in root.glob("*.json"):
        with locked(path.with_suffix(".lock")):
            state = read(path)
            if state.get("worktreePath") != worktree_path:
                continue
            state.update(enabled=False, stoppedReason="Stopped by review cleanup")
            save(path, state)
            stopped += 1
    return stopped


def running(path):
    try:
        with locked(path.with_suffix(".worker.lock"), blocking=False):
            return False
    except BlockingIOError:
        return True


def ensure(path):
    if not read(path).get("enabled") or running(path):
        return
    with path.with_suffix(".log").open("a") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(path)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
            cwd=Path.home())
    # A worker lock, not a reusable PID, proves startup and prevents duplicates.
    deadline = time.monotonic() + 5
    while not running(path):
        if process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"T3 monitor failed to start; see {path.with_suffix('.log')}")
        time.sleep(0.05)


class GitHub:
    def __init__(self, host, binary="gh"):
        self.host = host
        self.binary = binary
        self.login = self.get("user")["login"]

    def get(self, endpoint, paginated=False):
        command = [self.binary, "api", "--hostname", self.host, endpoint]
        if paginated:
            command += ["--paginate", "--slurp"]
        result = subprocess.run(command, text=True, capture_output=True, timeout=90)
        if result.returncode:
            # Do not log credentials, subprocess environment, or response bodies.
            raise RuntimeError(f"gh api failed for {self.host}/{endpoint} (exit {result.returncode})")
        data = json.loads(result.stdout)
        return [item for page in data for item in page] if paginated else data

    def poll(self, state):
        repo = state["repo"]
        number = state["number"]
        pull = self.get(f"repos/{repo}/pulls/{number}")
        since = urllib.parse.quote(state["since"], safe="")
        feedback = {}
        endpoints = {
            "comment": f"repos/{repo}/issues/{number}/comments?per_page=100&since={since}",
            "review comment": f"repos/{repo}/pulls/{number}/comments?per_page=100&since={since}",
            "review": f"repos/{repo}/pulls/{number}/reviews?per_page=100",
        }
        for kind, endpoint in endpoints.items():
            for item in self.get(endpoint, paginated=True):
                stamp = item.get("updated_at") or item.get("submitted_at") or item.get("created_at")
                author = (item.get("user") or {}).get("login")
                if not stamp or stamp < state["since"] or author == self.login or item.get("state") == "PENDING":
                    continue
                key = f"{kind} {item['id']}"
                feedback[key] = f"{key} by {author} at {stamp} ({item.get('state', 'posted')})"
        return {"head": pull["head"]["sha"],
            "closed": "merged" if pull.get("merged") else "closed" if pull["state"] == "closed" else None,
            "feedback": feedback}


def observe(state, update):
    if state["head"] != update["head"]:
        state["pending"]["head"] = f"New PR head: {state['head']} -> {update['head']}"
    if state["closed"] != update["closed"]:
        state["pending"]["lifecycle"] = "PR " + (update["closed"] or "reopened")
    for key, value in update["feedback"].items():
        if state["feedback"].get(key) != value:
            state["pending"][key] = value
    state["head"] = update["head"]
    state["closed"] = update["closed"]
    state["feedback"].update(update["feedback"])


def busy(thread):
    session = thread.get("session") or {}
    return ((thread.get("latestTurn") or {}).get("state") == "running"
        or session.get("activeTurnId") or session.get("status") in ("starting", "running"))


def prompt(state):
    changes = list(state["pending"].values())
    if state.get("resumePrompt"):
        return state["resumePrompt"] + "\n\nWatcher updates:\n" + "\n".join(changes[:50])
    return (f"The development host PR watcher detected updates for {state['prUrl']}:\n"
        + "\n".join("- " + event for event in changes[:50])
        + (f"\n- {len(changes) - 50} additional updates; fetch all current feedback." if len(changes) > 50 else "")
        + "\n\nResume the autonomous review cycle from your original instructions. Verify the current PR "
        "state with gh. If open, update the dedicated worktree, review the delta and new feedback with "
        "the original methodology and posting rules, and post only when something material changed. "
        "If merged or closed, follow the original safe cleanup instructions. "
        "The external development host watcher manages subsequent wakeups through T3; do not launch or re-arm "
        "a background shell monitor. Finish this turn after handling the update.")


def deliver(client, state, persist, timestamp):
    # Snapshot includes deleted shells; the thread endpoint does not.
    threads = client.request("/api/orchestration/snapshot")["threads"]
    shell = next((t for t in threads if t["id"] == state["threadId"]), None)
    if shell is None or shell.get("deletedAt") or shell.get("archivedAt"):
        state.update(enabled=False, stoppedReason="Thread deleted or archived")
        persist()
        return
    thread = client.request(f"/api/orchestration/threads/{state['threadId']}?turnLimit=1")["thread"]
    latest = thread.get("latestTurn") or {}
    session = thread.get("session") or {}
    delivery = state.get("delivery")
    if delivery:
        seen = any(m["id"] == delivery["messageId"] for m in thread.get("messages", []))
        if (seen and latest.get("turnId") and latest["turnId"] != delivery["previousTurnId"]
                and latest.get("state") in ("running", "completed")):
            for key, value in delivery["events"].items():
                if state["pending"].get(key) == value:
                    del state["pending"][key]
            state.update(delivery=None, lastDeliveredAt=timestamp, lastTurnId=latest["turnId"], lastError=None)
            if delivery["closed"] and state["closed"] == delivery["closed"]:
                state.update(enabled=False, stoppedReason="PR " + state["closed"])
            persist()
            return
        if (session.get("status") == "error" and session.get("updatedAt", "") >= delivery["createdAt"]
                or seen and latest.get("turnId") != delivery["previousTurnId"] and latest.get("state") == "error"):
            # A rejected provider start has a durable command receipt. Retry with
            # a fresh command; replaying the rejected start would never wake T3.
            state.update(delivery=None, lastError=session.get("lastError") or "T3 turn failed to start")
            persist()
            return
    if busy(thread) or not state["pending"] or not state["enabled"]:
        return
    if delivery is None:
        delivery = {"commandId": str(uuid.uuid4()), "messageId": str(uuid.uuid4()),
            "previousTurnId": latest.get("turnId"), "createdAt": timestamp,
            "events": dict(state["pending"]), "closed": state["closed"], "text": prompt(state)}
        state["delivery"] = delivery
        # Persist before HTTP: retry the SAME command after a crash or lost ack.
        persist()
    client.dispatch("thread.turn.start", commandId=delivery["commandId"], threadId=state["threadId"],
        message={"messageId": delivery["messageId"], "role": "user", "text": delivery["text"], "attachments": []},
        modelSelection=thread["modelSelection"], runtimeMode=thread["runtimeMode"],
        interactionMode="default", createdAt=delivery["createdAt"])


def worker(path):
    spec = importlib.util.spec_from_file_location("t3_review", Path(__file__).with_name("t3-review.py"))
    t3 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t3)
    interval = max(1, float(os.environ.get("AGENT_PR_REVIEW_MONITOR_INTERVAL", "30")))
    github = None
    sources = [Path(__file__), Path(__file__).with_name("t3-review.py")]
    version = [source.stat().st_mtime_ns for source in sources]
    with locked(path.with_suffix(".worker.lock"), blocking=False):
        while path.exists():
            try:
                if [source.stat().st_mtime_ns for source in sources] != version:
                    # The installer deploys copies. Load updates without killing
                    # a review or forgetting queued events; exec releases flock.
                    os.execv(sys.executable, [sys.executable, str(sources[0]), "--worker", str(path)])
                state = read(path)
                if not state["enabled"]:
                    return
                if github is None:
                    github = GitHub(state["host"], state.get("ghPath", "gh"))
                update = github.poll(state)
                with locked(path.with_suffix(".lock")):
                    state = read(path)
                    observe(state, update)
                    state.update(lastPollAt=t3.now(), pid=os.getpid(), lastError=None)
                    save(path, state)
                # Same lock order as extension launches: launch, then state.
                with locked(path.parent.parent / "t3-launch.lock"), locked(path.with_suffix(".lock")):
                    state = read(path)
                    if not state["enabled"]:
                        return
                    with t3.connect(Path(state["baseDir"])) as client:
                        deliver(client, state, lambda: save(path, state), t3.now())
                    state["lastCheckAt"] = t3.now()
                    save(path, state)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                if not path.exists():
                    return
                with locked(path.with_suffix(".lock")):
                    state = read(path)
                    message = str(error)
                    if message != state.get("lastError"):
                        print(f"{t3.now()} {message}", flush=True)
                    state.update(lastError=message, lastErrorAt=t3.now())
                    save(path, state)
            time.sleep(interval)


def install_cron(root):
    # Optional cron supervision covers only our saved watchers;
    # flock prevents overlap, and each worker survives disconnects on its own.
    marker = "# agent-pr-review T3 monitor " + str(root)
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode and "no crontab" not in result.stderr.lower():
        raise RuntimeError("Cannot read crontab; existing jobs left unchanged")
    lines = [line for line in result.stdout.splitlines() if not line.endswith(marker)]
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "--directory", str(root), "--ensure"])
    command = command.replace("%", r"\%")
    lines.append(f"* * * * * {command} >/dev/null 2>&1 {marker}")
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=directory())
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--worker", type=Path)
    actions.add_argument("--ensure", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--stop", metavar="THREAD_ID")
    actions.add_argument("--stop-worktree", metavar="PATH")
    actions.add_argument("--install-cron", action="store_true")
    args = parser.parse_args()
    root = args.directory.expanduser().resolve()
    if args.worker:
        try:
            worker(args.worker.resolve())
        except BlockingIOError:
            pass  # Another launcher/cron invocation already owns this watcher.
    elif args.install_cron:
        install_cron(root)
    elif args.stop:
        path = root / (str(uuid.UUID(args.stop)) + ".json")
        with locked(path.with_suffix(".lock")):
            state = read(path)
            state.update(enabled=False, stoppedReason="Stopped by user")
            save(path, state)
    elif args.stop_worktree:
        if not stop_worktree(root, args.stop_worktree):
            raise SystemExit(f"No saved watcher for worktree {args.stop_worktree}")
    else:
        for path in sorted(root.glob("*.json")):
            if args.ensure:
                ensure(path)
            else:
                state = read(path)
                print(json.dumps({"threadId": state["threadId"], "prUrl": state["prUrl"],
                    "enabled": state["enabled"], "running": running(path),
                    "pending": len(state["pending"]), **{k: state.get(k) for k in
                    ("lastPollAt", "lastCheckAt", "lastDeliveredAt", "lastTurnId", "lastError", "stoppedReason")}}))


if __name__ == "__main__":
    main()
