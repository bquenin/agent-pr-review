#!/usr/bin/env python3
"""Start/resume reviews through the local T3 server's authenticated HTTP API."""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from review_config import load, parse_pr


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def identity(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Client:
    def __init__(self, origin, token):
        url = urllib.parse.urlsplit(origin)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username:
            raise ValueError("T3 server-runtime.json must point to a loopback HTTP server")
        self.origin = origin.rstrip("/")
        self.token = token
        # Local requests must stay local even when a proxy is configured.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None):
        req = urllib.request.Request(self.origin + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            error.close()
            raise RuntimeError(f"T3 API {path}: HTTP {error.code}; check the server log") from None

    def dispatch(self, kind, **fields):
        return self.request("/api/orchestration/dispatch", {
            "type": kind, "commandId": str(uuid.uuid4()), **fields})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("T3 API unexpectedly redirected a local authenticated request")


def t3_command(runtime):
    override = os.environ.get("AGENT_PR_REVIEW_T3_BIN")
    if override:
        return [override]

    # Use the running server's exact version, even after an upgrade on disk.
    pid = int(runtime["pid"])
    if sys.platform == "darwin":
        return t3_command_darwin(pid)
    process = Path(f"/proc/{pid}")
    binary = os.readlink(process / "exe")
    if Path(binary).name not in ("node", "nodejs"):
        return [binary]

    # Source runtimes exec node <entrypoint> serve ...; /proc/PID/exe is
    # only the interpreter. Keep the script, but not the server arguments.
    argv = [os.fsdecode(arg) for arg in (process / "cmdline").read_bytes().split(b"\0")]
    if len(argv) < 2 or not argv[1] or argv[1].startswith("-"):
        raise RuntimeError("Cannot discover the Node-hosted T3 entrypoint; set AGENT_PR_REVIEW_T3_BIN to its t3 wrapper")
    script = Path(argv[1])
    if not script.is_absolute():
        script = Path(os.readlink(process / "cwd")) / script
    if not script.is_file():
        raise RuntimeError("The running T3 entrypoint is missing; restart T3 or set AGENT_PR_REVIEW_T3_BIN")
    return [binary, str(script)]


def t3_command_darwin(pid):
    # The desktop app runs Electron as Node on its server entrypoint:
    #   <bundle>/Contents/MacOS/<name> <bundle>/.../apps/server/dist/bin.mjs --bootstrap-fd N
    # ps joins arguments with spaces, so recover the entrypoint by stripping the
    # executable prefix and the trailing flags rather than splitting on spaces.
    def ps(column):
        return subprocess.check_output(["/bin/ps", "-ww", "-o", f"{column}=", "-p", str(pid)], timeout=10).decode().strip()
    binary, args = ps("comm"), ps("args")
    if not binary or not Path(binary).is_file():
        raise RuntimeError("The running T3 server process was not found; restart T3 or set AGENT_PR_REVIEW_T3_BIN")
    def exists(path):
        # Packaged builds load the entrypoint from inside app.asar, which Electron
        # reads transparently but which is a single archive file on disk.
        archive = next((parent for parent in path.parents if parent.suffix == ".asar"), None)
        return archive.is_file() if archive else path.is_file()
    rest = args[len(binary):].strip() if args.startswith(binary) else args
    tokens = rest.split(" ")
    script = next((candidate for candidate in (Path(" ".join(tokens[:count])) for count in range(1, len(tokens) + 1))
                   if candidate.is_absolute() and exists(candidate)), None)
    if script is None:
        raise RuntimeError("Cannot discover the T3 server entrypoint; set AGENT_PR_REVIEW_T3_BIN to its t3 wrapper")
    if Path(binary).name not in ("node", "nodejs"):
        os.environ["ELECTRON_RUN_AS_NODE"] = "1"
    return [binary, str(script)]


@contextlib.contextmanager
def connect(base):
    runtime = read_json(base / "userdata/server-runtime.json")
    command = t3_command(runtime) + ["auth", "session"]
    issued = json.loads(subprocess.check_output(command + ["issue", "--base-dir", str(base),
        "--ttl", "5m", "--label", "agent-pr-review", "--json"], timeout=30))
    try:
        yield Client(runtime["origin"], issued["token"])
    finally:
        try:
            subprocess.run(command + ["revoke", issued["sessionId"], "--base-dir", str(base)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError):
            print("Warning: temporary T3 session could not be revoked; it expires in five minutes.", file=sys.stderr)


def start_turn(client, thread_id, previous_turn_id=None, **fields):
    submitted_at = now()
    client.dispatch("thread.turn.start", threadId=thread_id, **fields)
    # Dispatch acknowledges the command before the provider starts. Surface
    # asynchronous startup failures to the Mac handler instead of reporting success.
    deadline = time.monotonic() + 30
    while True:
        thread = client.request(f"/api/orchestration/threads/{thread_id}?turnLimit=1")["thread"]
        session = thread.get("session") or {}
        latest = thread.get("latestTurn") or {}
        if session.get("status") == "error" and session.get("updatedAt", "") >= submitted_at:
            raise RuntimeError(session.get("lastError") or "T3 provider failed to start")
        if latest.get("turnId") and latest["turnId"] != previous_turn_id:
            if latest.get("state") == "error":
                raise RuntimeError(session.get("lastError") or "T3 review turn failed; open the thread for details")
            if latest.get("state") in ("running", "completed"):
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("T3 created the thread but the provider has not started after 30 seconds; open the review thread for details")
        time.sleep(0.5)


def select_project(projects, root, config):
    active = [project for project in projects if not project.get("deletedAt")]
    if "projectId" in config:
        project_id = config["projectId"]
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("projectId in ~/.config/agent-pr-review/t3.json must be a non-empty project ID")
        project = next((p for p in active if p["id"] == project_id), None)
        if project is None:
            raise RuntimeError(f"Configured T3 project {project_id!r} is missing or deleted; update ~/.config/agent-pr-review/t3.json")
        return project
    return next((p for p in active if p["workspaceRoot"] == root), None)


def review_thread_id(threads, root, pr_url, environment_id):
    thread_id = identity(f"agent-pr-review:{environment_id}:{root}:{pr_url}")
    # Deleted threads stay in the event log. Give a restarted review a new identity.
    while thread_id in threads and threads[thread_id].get("deletedAt"):
        thread_id = identity(thread_id + ":" + threads[thread_id]["deletedAt"])
    return thread_id


def review_status(client, pr_url, environment_id):
    # Read only: no git fetch, worktree creation, watcher registration or dispatch.
    if not isinstance(pr_url, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", pr_url):
        raise ValueError("Expected a canonical HTTPS PR URL")
    snapshot = client.request("/api/orchestration/snapshot")
    threads = {thread["id"]: thread for thread in snapshot["threads"]}
    roots = set()
    for thread in threads.values():
        root, marker, _ = (thread.get("worktreePath") or "").rpartition("/.agent-pr-review/worktrees/")
        if marker:
            roots.add(root)
    matches = [threads[thread_id] for root in roots
        if (thread_id := review_thread_id(threads, root, pr_url, environment_id)) in threads]
    if not matches:
        return {"state": "missing"}
    # Prefer a nonarchived review when there are multiple clones of this repo.
    thread = min(matches, key=lambda item: bool(item.get("archivedAt")))
    latest = thread.get("latestTurn") or {}
    session = thread.get("session") or {}
    state = "exists"
    if thread.get("archivedAt"):
        state = "archived"
    elif latest.get("state") == "running" or session.get("activeTurnId") or session.get("status") in ("starting", "running"):
        state = "running"
    elif latest.get("state") == "error" or session.get("status") == "error" or not latest:
        state = "error"
    return {"state": state}


def launch(client, payload, settings, config, environment_id):
    snapshot = client.request("/api/orchestration/snapshot")
    root = payload["repoPath"]
    threads = {thread["id"]: thread for thread in snapshot["threads"]}
    thread_id = review_thread_id(threads, root, payload["prUrl"], environment_id)
    thread = threads.get(thread_id)
    if thread:
        if thread.get("archivedAt"):
            client.dispatch("thread.unarchive", threadId=thread_id)
        latest = thread.get("latestTurn") or {}
        session = thread.get("session") or {}
        if latest.get("state") == "running" or session.get("activeTurnId") or session.get("status") in ("starting", "running"):
            return {"threadId": thread_id, "action": "already-running", "title": thread["title"]}
        # A completed review is waiting on the external watcher, not a new prompt.
        # A stopped turn in a still-ready session needs no recovery either.
        # main() still ensures its watcher is running, even after a host restart.
        if (latest.get("state") == "completed" and session.get("status") != "error"
                or latest.get("state") == "interrupted" and session.get("status") == "ready"):
            return {"threadId": thread_id, "action": "reused", "title": thread["title"]}
        start_turn(client, thread_id, previous_turn_id=latest.get("turnId"),
            message={"messageId": str(uuid.uuid4()), "role": "user", "text": payload["resumePrompt"] if thread.get("latestTurn") else payload["prompt"], "attachments": []},
            runtimeMode=thread["runtimeMode"], interactionMode="default", createdAt=now())
        return {"threadId": thread_id, "action": "resumed", "title": thread["title"]}

    project = select_project(snapshot["projects"], root, config)
    if project is None:
        project = {"id": str(uuid.uuid4())}
        client.dispatch("project.create", projectId=project["id"], title=Path(root).name,
                        workspaceRoot=root, createdAt=now())
    overrides = settings.get("projectSettingsOverrides", {}).get(project["id"], {})
    model = (config.get("modelSelection") or overrides.get("defaultModelSelection")
             or project.get("defaultModelSelection") or settings.get("defaultModelSelection")
             or payload["defaultModelSelection"])
    if not model:
        raise ValueError("Choose a model in T3 settings, t3.json, or config.json (t3_model)")
    runtime = config.get("runtimeMode") or overrides.get("defaultRuntimeMode") or settings.get("defaultRuntimeMode", "full-access")
    title = payload["title"]
    # Bootstrap is implemented by T3's WebSocket handler, not HTTP dispatch.
    # HTTP clients must explicitly create the thread before submitting a turn.
    client.dispatch("thread.create", threadId=thread_id, projectId=project["id"], title=title,
        modelSelection=model, runtimeMode=runtime, interactionMode="default",
        branch=payload["branch"], worktreePath=payload["worktreePath"], createdAt=now())
    start_turn(client, thread_id,
        message={"messageId": str(uuid.uuid4()), "role": "user", "text": payload["prompt"], "attachments": []},
        modelSelection=model, runtimeMode=runtime, interactionMode="default", createdAt=now())
    return {"threadId": thread_id, "action": "started", "title": title}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="Verify the running server and read its project count; start no review")
    actions.add_argument("--print-cmd", action="store_true", help="Print the prepared request without contacting T3")
    actions.add_argument("--status", action="store_true", help="Read PR review status from a JSON prUrl on stdin; start nothing")
    args = parser.parse_args()
    if args.print_cmd:
        print(json.dumps(json.load(sys.stdin), indent=2))
        return
    base = Path(os.environ.get("T3CODE_HOME", str(Path.home() / ".t3"))).expanduser()
    if args.check:
        with connect(base) as client:
            snapshot = client.request("/api/orchestration/snapshot")
            print(f"T3 ready: {client.origin}, {len(snapshot['projects'])} projects")
        return
    payload = json.load(sys.stdin)
    parse_pr(payload["prUrl"], load())
    if args.status:
        environment_id = (base / "userdata/environment-id").read_text().strip()
        with connect(base) as client:
            print(json.dumps(review_status(client, payload["prUrl"], environment_id)))
        return
    # Capture the cursor before launch: feedback arriving during the initial
    # review must still wake the thread once it finishes.
    monitor_since = now().split(".")[0] + "Z"
    config = read_json(Path.home() / ".config/agent-pr-review/t3.json", {})
    settings = read_json(base / "userdata/settings.json", {})
    environment_id = (base / "userdata/environment-id").read_text().strip()
    lock_dir = Path(os.environ.get("AGENT_PR_REVIEW_RESOURCES", str(Path.home() / ".local/share/agent-pr-review")))
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "t3-launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with connect(base) as client:
            result = launch(client, payload, settings, config, environment_id)
        from t3_monitor import register, stop
        monitor_path = None
        if payload.get("monitorEnabled", False):
            monitor_path = register(lock_dir / "t3-monitors", base, payload, result["threadId"], monitor_since)
        else:
            stop(lock_dir / "t3-monitors", result["threadId"])
        print(json.dumps({**result, "monitor": str(monitor_path) if monitor_path else None}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"agent-pr-review: T3 launch failed: {error}", file=sys.stderr)
        sys.exit(1)
