import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import uuid
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, HTTPServer

spec = importlib.util.spec_from_file_location("t3_review", Path(__file__).parents[1] / "vm/t3-review.py")
t3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t3)

PAYLOAD = {"repoPath": "/code/service", "prUrl": "https://github.com/team/service/pull/42",
    "worktreePath": "/code/service/.agent-pr-review/worktrees/pr-42", "branch": "review/pr-42",
    "monitorEnabled": True, "title": "Review service #42", "prompt": "Review `code`\n$(do not execute) '",
    "resumePrompt": "Check for changes", "defaultModelSelection": {"instanceId": "cursor", "model": "grok-4.6",
        "options": [{"id": "reasoning", "value": "xhigh"}]}}


class FakeClient:
    def __init__(self):
        self.snapshot = {"projects": [], "threads": []}
        self.commands = []

    def request(self, path):
        if path == "/api/orchestration/snapshot":
            return copy.deepcopy(self.snapshot)
        thread_id = path.split("/")[-1].split("?")[0]
        return {"thread": copy.deepcopy(next(t for t in self.snapshot["threads"] if t["id"] == thread_id))}

    def dispatch(self, kind, **fields):
        self.commands.append({"type": kind, **fields})
        if kind == "project.create":
            self.snapshot["projects"].append({"id": fields["projectId"], "workspaceRoot": fields["workspaceRoot"]})
        if kind == "thread.create":
            self.snapshot["threads"].append({"id": fields["threadId"], **fields, "latestTurn": None})
        if kind == "thread.unarchive":
            thread = next(t for t in self.snapshot["threads"] if t["id"] == fields["threadId"])
            thread["archivedAt"] = None
        if kind == "thread.turn.start":
            thread = next((t for t in self.snapshot["threads"] if t["id"] == fields["threadId"]), None)
            if thread is None:
                raise RuntimeError("Thread does not exist for command thread.turn.start")
            thread["latestTurn"] = {"state": "running", "turnId": str(uuid.uuid4())}


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(t3.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.runtime = {"pid": 123, "origin": "http://127.0.0.1:3774"}

    def test_native_runtime_uses_running_binary(self):
        with patch.object(t3.os, "readlink", return_value="/runtime/0.0.42/t3"), \
                patch.object(Path, "read_bytes") as cmdline:
            self.assertEqual(t3.t3_command(self.runtime), ["/runtime/0.0.42/t3"])
        cmdline.assert_not_called()

    def test_explicit_wrapper_override_does_not_require_proc(self):
        with patch.dict(t3.os.environ, {"AGENT_PR_REVIEW_T3_BIN": "/custom path/t3"}), \
                patch.object(t3.os, "readlink") as readlink:
            self.assertEqual(t3.t3_command(self.runtime), ["/custom path/t3"])
        readlink.assert_not_called()

    def test_node_auth_includes_entrypoint_and_revokes_after_request_failure(self):
        script = "/runtime/source build/dist/bin.mjs"
        cmdline = "\0".join(["node", script, "serve", "--port", "3774", ""]).encode()
        base = Path("/home/coder/.t3")
        command = ["/pinned/bin/node", script, "auth", "session"]
        with patch.object(t3, "read_json", return_value=self.runtime), \
                patch.object(t3.os, "readlink", return_value="/pinned/bin/node"), \
                patch.object(Path, "read_bytes", return_value=cmdline), \
                patch.object(Path, "is_file", return_value=True), \
                patch.object(t3.subprocess, "check_output", return_value=b'{"token":"test-token","sessionId":"test-session"}') as issue, \
                patch.object(t3.subprocess, "run") as revoke:
            with self.assertRaisesRegex(RuntimeError, "request failed"):
                with t3.connect(base) as client:
                    self.assertEqual(client.origin, self.runtime["origin"])
                    self.assertEqual(client.token, "test-token")
                    raise RuntimeError("request failed")
        issue.assert_called_once_with(command + ["issue", "--base-dir", str(base),
            "--ttl", "5m", "--label", "agent-pr-review", "--json"], timeout=30)
        revoke.assert_called_once_with(command + ["revoke", "test-session", "--base-dir", str(base)],
            stdout=t3.subprocess.DEVNULL, stderr=t3.subprocess.DEVNULL, timeout=30, check=True)

    def test_relative_script_uses_server_working_directory(self):
        with patch.object(t3.os, "readlink", side_effect=["/usr/bin/nodejs", "/source"]), \
                patch.object(Path, "read_bytes", return_value=b"node\0dist/bin.mjs\0serve\0"), \
                patch.object(Path, "is_file", return_value=True):
            self.assertEqual(t3.t3_command(self.runtime), ["/usr/bin/nodejs", "/source/dist/bin.mjs"])

    def test_unsupported_node_commands_offer_explicit_override(self):
        for cmdline in (b"", b"node\0", b"node\0--import\0loader.mjs\0dist/bin.mjs\0serve\0"):
            with self.subTest(cmdline=cmdline), \
                    patch.object(t3.os, "readlink", return_value="/usr/bin/node"), \
                    patch.object(Path, "read_bytes", return_value=cmdline):
                with self.assertRaisesRegex(RuntimeError, "AGENT_PR_REVIEW_T3_BIN"):
                    t3.t3_command(self.runtime)

    def test_missing_script_reports_recovery_instead_of_executing_node(self):
        with patch.object(t3.os, "readlink", return_value="/usr/bin/node"), \
                patch.object(Path, "read_bytes", return_value=b"node\0/missing/bin.mjs\0serve\0"), \
                patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "entrypoint is missing"):
                t3.t3_command(self.runtime)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        settings = patch.object(t3, "load", return_value={"github_hosts": ["github.com"]})
        settings.start()
        self.addCleanup(settings.stop)

    def launch(self, payload=PAYLOAD, settings=None, config=None):
        return t3.launch(self.client, payload, settings or {}, config or {}, "dev-environment")

    def test_first_launch_and_duplicate_click(self):
        first = self.launch()
        self.assertEqual(first["action"], "started")
        self.assertEqual([c["type"] for c in self.client.commands], ["project.create", "thread.create", "thread.turn.start"])
        turn = self.client.commands[-1]
        self.assertEqual(turn["message"]["text"], PAYLOAD["prompt"])
        self.assertEqual(turn["modelSelection"], PAYLOAD["defaultModelSelection"])
        self.assertEqual(self.client.commands[-2]["modelSelection"], PAYLOAD["defaultModelSelection"])
        self.assertEqual(turn["runtimeMode"], "full-access")
        self.assertEqual(self.client.commands[-2]["worktreePath"], PAYLOAD["worktreePath"])
        self.assertEqual(self.launch()["action"], "already-running")
        self.assertEqual(len(self.client.commands), 3)

    def test_completed_review_is_reused_without_another_turn(self):
        first = self.launch()
        thread = self.client.snapshot["threads"][0]
        # The persisted review survives idle/stopped provider processes too.
        for session in (None, {"status": "ready"}, {"status": "idle"}, {"status": "stopped"}):
            with self.subTest(session=session):
                thread["latestTurn"]["state"] = "completed"
                thread["session"] = session
                previous = copy.deepcopy(self.client.snapshot)
                result = self.launch()
                self.assertEqual(result["threadId"], first["threadId"])
                self.assertEqual(result["action"], "reused")
                self.assertEqual(len(self.client.commands), 3)
                self.assertEqual(self.client.snapshot, previous)

    def test_archived_completed_review_is_unarchived_without_another_turn(self):
        first = self.launch()
        self.client.snapshot["threads"][0].update(latestTurn={"state": "completed"}, archivedAt="yesterday")
        result = self.launch()
        self.assertEqual(result["threadId"], first["threadId"])
        self.assertEqual(result["action"], "reused")
        self.assertEqual(self.client.commands[-1]["type"], "thread.unarchive")
        self.assertEqual(self.launch()["action"], "reused")
        self.assertEqual(len(self.client.commands), 4)

    def test_interrupted_turn_with_ready_session_is_not_restarted(self):
        first = self.launch()
        thread = self.client.snapshot["threads"][0]
        thread["latestTurn"]["state"] = "interrupted"
        thread["session"] = {"status": "ready", "activeTurnId": None}
        previous = copy.deepcopy(self.client.snapshot)
        for _ in range(2):
            result = self.launch()
            self.assertEqual(result["threadId"], first["threadId"])
            self.assertEqual(result["action"], "reused")
        self.assertEqual(len(self.client.commands), 3)
        self.assertEqual(self.client.snapshot, previous)

    def test_failed_or_interrupted_review_resumes_existing_thread(self):
        first = self.launch()
        thread = self.client.snapshot["threads"][0]
        cases = (("error", "error"), ("error", "ready"), ("completed", "error"),
            ("interrupted", "stopped"), ("interrupted", "interrupted"), ("interrupted", None))
        for turn_state, session_status in cases:
            with self.subTest(turn_state=turn_state, session_status=session_status):
                thread["latestTurn"]["state"] = turn_state
                thread["session"] = {"status": session_status} if session_status else None
                count = len(self.client.commands)
                result = self.launch()
                self.assertEqual(result["threadId"], first["threadId"])
                self.assertEqual(result["action"], "resumed")
                self.assertEqual(len(self.client.commands), count + 1)
                self.assertEqual(self.client.commands[-1]["type"], "thread.turn.start")
                self.assertEqual(self.client.commands[-1]["message"]["text"], PAYLOAD["resumePrompt"])
                self.assertNotIn("bootstrap", self.client.commands[-1])

    def test_reusing_completed_review_still_registers_watcher(self):
        first = self.launch()
        self.client.snapshot["threads"][0]["latestTurn"]["state"] = "completed"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "userdata").mkdir()
            (base / "userdata/environment-id").write_text("dev-environment")
            resources = base / "resources"
            watcher = Mock()
            watcher.register.return_value = resources / "t3-monitors" / (first["threadId"] + ".json")
            with patch.dict(t3.os.environ, {"T3CODE_HOME": str(base), "AGENT_PR_REVIEW_RESOURCES": str(resources)}), \
                    patch.dict(t3.sys.modules, {"t3_monitor": watcher}), \
                    patch.object(t3, "read_json", return_value={}), \
                    patch.object(t3, "now", return_value="2026-09-19T04:00:00.000Z"), \
                    patch.object(t3, "connect") as connect, \
                    patch.object(t3.sys, "argv", ["t3-review.py"]), \
                    patch.object(t3.sys, "stdin", io.StringIO(json.dumps(PAYLOAD))), \
                    patch.object(t3.sys, "stdout", io.StringIO()) as output:
                connect.return_value.__enter__.return_value = self.client
                t3.main()
            result = json.loads(output.getvalue())
            watcher.register.assert_called_once_with(resources / "t3-monitors", base, PAYLOAD,
                first["threadId"], "2026-09-19T04:00:00Z")
        self.assertEqual(result["threadId"], first["threadId"])
        self.assertEqual(result["action"], "reused")
        self.assertEqual(len(self.client.commands), 3)

    def test_disabled_monitor_stops_existing_watcher_without_registering(self):
        self.launch()
        self.client.snapshot["threads"][0]["latestTurn"]["state"] = "completed"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "userdata").mkdir()
            (base / "userdata/environment-id").write_text("dev-environment")
            watcher = Mock()
            with patch.dict(t3.os.environ, {"T3CODE_HOME": str(base), "AGENT_PR_REVIEW_RESOURCES": str(base / "resources")}), \
                    patch.dict(t3.sys.modules, {"t3_monitor": watcher}), \
                    patch.object(t3, "read_json", return_value={}), \
                    patch.object(t3, "connect") as connect, \
                    patch.object(t3.sys, "argv", ["t3-review.py"]), \
                    patch.object(t3.sys, "stdin", io.StringIO(json.dumps(dict(PAYLOAD, monitorEnabled=False)))), \
                    patch.object(t3.sys, "stdout", io.StringIO()) as output:
                connect.return_value.__enter__.return_value = self.client
                t3.main()
            watcher.register.assert_not_called()
            watcher.stop.assert_called_once()
            self.assertIsNone(json.loads(output.getvalue())["monitor"])

    def test_active_session_without_latest_turn_is_not_interrupted(self):
        self.launch()
        for session in ({"status": "starting"}, {"status": "running"}, {"status": "ready", "activeTurnId": "active"}):
            with self.subTest(session=session):
                self.client.snapshot["threads"][0].update(latestTurn=None, session=session)
                self.assertEqual(self.launch()["action"], "already-running")
                self.assertEqual(len(self.client.commands), 3)

    def test_deleted_reviews_get_new_threads(self):
        first = self.launch()
        self.client.snapshot["threads"][0]["deletedAt"] = "yesterday"
        second = self.launch()
        self.client.snapshot["threads"][1]["deletedAt"] = "yesterday"
        third = self.launch()
        self.assertEqual(len({first["threadId"], second["threadId"], third["threadId"]}), 3)
        self.assertEqual(len(self.client.snapshot["projects"]), 1)

    def test_same_pr_on_different_github_hosts_has_separate_thread(self):
        first = self.launch()
        other = dict(PAYLOAD, prUrl=PAYLOAD["prUrl"].replace("github.com", "github.other"))
        self.assertNotEqual(first["threadId"], self.launch(other)["threadId"])

    def test_reuses_project_and_applies_model_override(self):
        self.client.snapshot["projects"] = [{"id": "existing", "workspaceRoot": PAYLOAD["repoPath"]}]
        model = {"instanceId": "claudeAgent_work", "model": "my-model"}
        self.launch(settings={"projectSettingsOverrides": {"existing": {"defaultModelSelection": model, "defaultRuntimeMode": "auto-accept-edits"}}})
        turn = self.client.commands[0]
        self.assertEqual(turn["modelSelection"], model)
        self.assertEqual(turn["runtimeMode"], "auto-accept-edits")
        self.assertEqual(turn["projectId"], "existing")

    def test_configured_project_wins_over_repo_project_and_keeps_pr_worktree(self):
        self.client.snapshot["projects"] = [
            {"id": "repo", "workspaceRoot": PAYLOAD["repoPath"]},
            {"id": "dev-project", "workspaceRoot": "/code"}]
        model = {"instanceId": "cursor", "model": "project-model"}
        self.launch(config={"projectId": "dev-project"}, settings={"projectSettingsOverrides": {
            "dev-project": {"defaultModelSelection": model, "defaultRuntimeMode": "auto-accept-edits"}}})
        self.assertEqual([c["type"] for c in self.client.commands], ["thread.create", "thread.turn.start"])
        created = self.client.commands[0]
        self.assertEqual(created["projectId"], "dev-project")
        self.assertEqual(created["worktreePath"], PAYLOAD["worktreePath"])
        self.assertEqual(created["branch"], PAYLOAD["branch"])
        self.assertEqual(created["modelSelection"], model)
        self.assertEqual(created["runtimeMode"], "auto-accept-edits")

    def test_reviews_from_different_repos_share_configured_project(self):
        self.client.snapshot["projects"] = [{"id": "dev-project", "workspaceRoot": "/code"}]
        first = self.launch(config={"projectId": "dev-project"})
        other = dict(PAYLOAD, repoPath="/code/other", prUrl="https://github.com/team/other/pull/42",
            worktreePath="/code/other/.agent-pr-review/worktrees/pr-42")
        second = self.launch(payload=other, config={"projectId": "dev-project"})
        self.assertNotEqual(first["threadId"], second["threadId"])
        self.assertEqual(len(self.client.snapshot["projects"]), 1)
        self.assertEqual([t["projectId"] for t in self.client.snapshot["threads"]], ["dev-project", "dev-project"])
        self.assertEqual(self.launch(config={"projectId": "dev-project"})["action"], "already-running")
        self.assertEqual(len(self.client.commands), 4)

    def test_invalid_configured_project_never_silently_creates_another(self):
        self.client.snapshot["projects"] = [{"id": "deleted", "workspaceRoot": "/code", "deletedAt": "yesterday"}]
        for project_id in ("missing", "deleted", "", None, 42):
            with self.subTest(project_id=project_id):
                with self.assertRaisesRegex((ValueError, RuntimeError), "t3.json"):
                    self.launch(config={"projectId": project_id})
                self.assertFalse(self.client.commands)

    def test_project_preference_does_not_duplicate_or_move_existing_review(self):
        first = self.launch()
        original_project = self.client.snapshot["threads"][0]["projectId"]
        self.client.snapshot["projects"].append({"id": "dev-project", "workspaceRoot": "/code"})
        self.client.snapshot["threads"][0]["latestTurn"]["state"] = "completed"
        result = self.launch(config={"projectId": "dev-project"})
        self.assertEqual(result["threadId"], first["threadId"])
        self.assertEqual(result["action"], "reused")
        self.assertEqual(len(self.client.snapshot["threads"]), 1)
        self.assertEqual(self.client.snapshot["threads"][0]["projectId"], original_project)

    def test_failed_first_turn_retries_with_full_review_instructions(self):
        dispatch = self.client.dispatch
        def fail_turn(kind, **fields):
            if kind == "thread.turn.start":
                raise RuntimeError("Turn submission failed")
            return dispatch(kind, **fields)
        self.client.dispatch = fail_turn
        with self.assertRaisesRegex(RuntimeError, "Turn submission failed"):
            self.launch()
        self.client.dispatch = dispatch
        result = self.launch()
        self.assertEqual(result["action"], "resumed")
        self.assertEqual(self.client.commands[-1]["message"]["text"], PAYLOAD["prompt"])
        self.assertEqual(len(self.client.snapshot["threads"]), 1)

    def test_asynchronous_provider_startup_error_reaches_launcher(self):
        dispatch = self.client.dispatch
        def reject_model(kind, **fields):
            result = dispatch(kind, **fields)
            if kind == "thread.turn.start":
                self.client.snapshot["threads"][0].update(latestTurn=None,
                    session={"status": "error", "updatedAt": t3.now(), "lastError": "Invalid model"})
            return result
        self.client.dispatch = reject_model
        with self.assertRaisesRegex(RuntimeError, "Invalid model"):
            self.launch()

    def test_api_failure_is_not_reported_as_success(self):
        def fail(*args, **kwargs):
            raise RuntimeError("server unavailable")
        self.client.dispatch = fail
        with self.assertRaisesRegex(RuntimeError, "server unavailable"):
            self.launch()


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        settings = patch.object(t3, "load", return_value={"github_hosts": ["github.com"]})
        settings.start()
        self.addCleanup(settings.stop)
        self.environment = "dev-environment"

    def launch(self):
        return t3.launch(self.client, PAYLOAD, {}, {}, self.environment)

    def status(self, url=PAYLOAD["prUrl"]):
        before = copy.deepcopy(self.client.snapshot)
        with patch.object(self.client, "dispatch", side_effect=AssertionError("Status must be read-only")):
            result = t3.review_status(self.client, url, self.environment)
        self.assertEqual(self.client.snapshot, before)
        return result["state"]

    def test_missing_running_completed_archived_and_failed_reviews(self):
        self.assertEqual(self.status(), "missing")
        self.launch()
        self.assertEqual(self.status(), "running")
        thread = self.client.snapshot["threads"][0]
        thread["latestTurn"]["state"] = "completed"
        self.assertEqual(self.status(), "exists")
        thread["latestTurn"]["state"] = "interrupted"
        thread["session"] = {"status": "ready"}
        self.assertEqual(self.status(), "exists")
        thread["session"]["status"] = "error"
        self.assertEqual(self.status(), "error")
        thread["archivedAt"] = "yesterday"
        self.assertEqual(self.status(), "archived")

    def test_deleted_threads_disappear_and_recreated_threads_are_found(self):
        self.launch()
        self.client.snapshot["threads"][0]["deletedAt"] = "yesterday"
        self.assertEqual(self.status(), "missing")
        self.launch()
        self.assertEqual(self.status(), "running")

    def test_matches_identity_not_thread_title_or_project(self):
        self.launch()
        self.client.snapshot["threads"][0].update(title="Renamed", projectId="dev-project")
        self.assertEqual(self.status(), "running")
        self.assertEqual(self.status(PAYLOAD["prUrl"] + "0"), "missing")
        self.assertEqual(self.status(PAYLOAD["prUrl"].replace("github.com", "github.other")), "missing")
        self.client.snapshot["threads"][0]["id"] = "unrelated"
        self.assertEqual(self.status(), "missing")

    def test_status_mode_does_not_start_turns_or_register_watchers(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "userdata").mkdir()
            (base / "userdata/environment-id").write_text(self.environment)
            with patch.dict(t3.os.environ, {"T3CODE_HOME": str(base), "AGENT_PR_REVIEW_RESOURCES": str(base / "resources")}), \
                    patch.object(t3.sys, "argv", ["t3-review.py", "--status"]), \
                    patch.object(t3.sys, "stdin", io.StringIO(json.dumps({"prUrl": PAYLOAD["prUrl"]}))), \
                    patch.object(t3.sys, "stdout", io.StringIO()) as output, \
                    patch.object(t3, "connect") as connect, \
                    patch.object(t3, "launch", side_effect=AssertionError("Must not launch")):
                connect.return_value.__enter__.return_value = self.client
                t3.main()
            self.assertEqual(json.loads(output.getvalue()), {"state": "missing"})
            self.assertEqual(list(base.iterdir()), [base / "userdata"])


class HttpTests(unittest.TestCase):
    def test_local_transport_and_errors(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"sequence":1}')
            def do_GET(self):
                self.send_response(401)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            client = t3.Client(f"http://127.0.0.1:{server.server_port}", "test-secret")
            client.dispatch("thread.turn.start", text=PAYLOAD["prompt"])
            self.assertEqual(seen[0][0], "Bearer test-secret")
            self.assertEqual(seen[0][1]["text"], PAYLOAD["prompt"])
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                client.request("/api/orchestration/snapshot")
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_credentials_cannot_go_to_remote_origin(self):
        for origin in ["https://example.com", "http://example.com", "http://user@localhost"]:
            with self.assertRaises(ValueError):
                t3.Client(origin, "test-secret")


if __name__ == "__main__":
    unittest.main()
