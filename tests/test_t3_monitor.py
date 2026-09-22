import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("t3_monitor", Path(__file__).parents[1] / "runtime/t3_monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

STAMP = "2026-09-19T04:00:00Z"


def state():
    return {"threadId": "17f60f03-3ff8-4b4f-8e3f-e638f396bf10", "prUrl": "https://github.example/team/repo/pull/42",
        "host": "github.example", "repo": "team/repo", "number": 42, "since": STAMP,
        "head": "old-head", "worktreePath": "/code/repo/.agent-pr-review/worktrees/pr-42",
        "feedback": {}, "pending": {}, "delivery": None, "closed": None, "enabled": True}


class Client:
    def __init__(self):
        self.thread = {"id": state()["threadId"], "latestTurn": {"turnId": "initial", "state": "completed"},
            "session": {"status": "ready"}, "messages": [], "runtimeMode": "full-access",
            "modelSelection": {"instanceId": "cursor", "model": "grok-4.6", "options": [{"id": "reasoning", "value": "xhigh"}]}}
        self.commands = []
        self.receipts = set()
        self.fail_after_accept = False

    def request(self, path):
        if path.endswith("snapshot"):
            return {"threads": [copy.deepcopy(self.thread)]}
        return {"thread": copy.deepcopy(self.thread)}

    def dispatch(self, kind, **fields):
        if fields["commandId"] in self.receipts:
            return
        self.receipts.add(fields["commandId"])
        self.commands.append({"type": kind, **fields})
        self.thread["messages"].append({"id": fields["message"]["messageId"]})
        self.thread["latestTurn"] = {"turnId": fields["commandId"], "state": "running"}
        if self.fail_after_accept:
            raise RuntimeError("Connection lost after dispatch")


class PolicyTests(unittest.TestCase):
    def test_registered_resume_instructions_override_original_policy(self):
        saved = state()
        saved.update(resumePrompt="Comment only. Never approve.", pending={"head": "New head"})
        self.assertIn("Comment only. Never approve.", monitor.prompt(saved))
        self.assertIn("New head", monitor.prompt(saved))

    def test_stop_disables_saved_watcher_without_removing_queued_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = state()
            target = root / (saved["threadId"] + ".json")
            monitor.save(target, saved)
            monitor.stop(root, saved["threadId"])
            self.assertFalse(monitor.read(target)["enabled"])
            self.assertEqual(monitor.read(target)["head"], saved["head"])

    def test_stop_worktree_disables_only_matching_watcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matching = state()
            other = dict(state(), threadId="27f60f03-3ff8-4b4f-8e3f-e638f396bf10",
                worktreePath="/code/other/.agent-pr-review/worktrees/pr-42")
            matching_path = root / (matching["threadId"] + ".json")
            other_path = root / (other["threadId"] + ".json")
            monitor.save(matching_path, matching)
            monitor.save(other_path, other)

            self.assertEqual(monitor.stop_worktree(root, matching["worktreePath"]), 1)

            stopped = monitor.read(matching_path)
            self.assertFalse(stopped["enabled"])
            self.assertEqual(stopped["stoppedReason"], "Stopped by review cleanup")
            self.assertTrue(monitor.read(other_path)["enabled"])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.state = state()
        self.client = Client()
        self.persisted = None

    def persist(self):
        self.persisted = copy.deepcopy(self.state)

    def tick(self):
        monitor.deliver(self.client, self.state, self.persist, STAMP)

    def update(self, head="new-head", feedback=None, closed=None):
        monitor.observe(self.state, {"head": head, "feedback": feedback or {}, "closed": closed})

    def test_commit_wakes_completed_thread_and_retains_model_and_access(self):
        self.update()
        self.tick()
        command = self.client.commands[0]
        self.assertEqual(command["threadId"], self.state["threadId"])
        self.assertEqual(command["modelSelection"], self.client.thread["modelSelection"])
        self.assertEqual(command["runtimeMode"], "full-access")
        self.assertIn("old-head -> new-head", command["message"]["text"])
        self.assertTrue(self.persisted["pending"])
        self.tick()
        self.assertFalse(self.state["pending"])
        self.assertEqual(self.state["lastTurnId"], command["commandId"])
        self.update()
        self.tick()
        self.assertEqual(len(self.client.commands), 1)

    def test_feedback_queues_while_busy_then_coalesces_into_one_turn(self):
        self.client.thread["session"] = {"status": "starting"}
        self.update(feedback={"comment 1": "new feedback"})
        self.tick()
        self.assertFalse(self.client.commands)
        self.update(head="newer-head", feedback={"comment 1": "edited feedback", "review 2": "new review"})
        self.client.thread["session"] = {"status": "ready"}
        self.tick()
        text = self.client.commands[0]["message"]["text"]
        self.assertIn("newer-head", text)
        self.assertIn("edited feedback", text)
        self.assertIn("new review", text)

    def test_restart_after_lost_ack_does_not_start_duplicate_turn(self):
        self.update()
        self.client.fail_after_accept = True
        with self.assertRaisesRegex(RuntimeError, "Connection lost"):
            self.tick()
        # Reload only data written BEFORE the failed HTTP request.
        self.state = copy.deepcopy(self.persisted)
        self.client.thread["latestTurn"]["state"] = "completed"
        self.tick()
        self.assertEqual(len(self.client.commands), 1)
        self.assertFalse(self.state["pending"])

    def test_retry_after_failed_request_preserves_command_identity(self):
        self.update()
        dispatch = self.client.dispatch
        with patch.object(self.client, "dispatch", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                self.tick()
        command_id = self.persisted["delivery"]["commandId"]
        self.state = copy.deepcopy(self.persisted)
        self.tick()
        self.assertEqual(self.client.commands[0]["commandId"], command_id)

    def test_startup_failure_retries_with_fresh_command_and_keeps_events(self):
        self.update()
        self.tick()
        original = self.client.commands[0]["commandId"]
        self.client.thread.update(latestTurn=None, session={"status": "error", "updatedAt": STAMP, "lastError": "Unavailable"})
        self.tick()
        self.assertIsNone(self.state["delivery"])
        self.assertTrue(self.state["pending"])
        self.tick()
        self.assertNotEqual(self.client.commands[-1]["commandId"], original)

    def test_changes_while_delivery_is_in_flight_are_not_acknowledged_early(self):
        self.update()
        self.tick()
        self.update(head="third-head")
        self.tick()
        self.assertIn("third-head", self.state["pending"]["head"])
        self.tick()
        self.assertEqual(len(self.client.commands), 1)
        self.client.thread["latestTurn"]["state"] = "completed"
        self.tick()
        self.assertEqual(len(self.client.commands), 2)

    def test_merge_wakes_final_turn_before_stopping(self):
        self.update(head="old-head", closed="merged")
        self.tick()
        self.assertTrue(self.state["enabled"])
        self.tick()
        self.assertFalse(self.state["enabled"])
        self.assertEqual(self.state["stoppedReason"], "PR merged")
        self.assertIn("safe cleanup", self.client.commands[0]["message"]["text"])

    def test_archived_and_deleted_threads_stop_without_waking(self):
        for field in ["archivedAt", "deletedAt"]:
            self.state = state()
            self.update()
            self.client.thread[field] = STAMP
            self.tick()
            self.assertFalse(self.state["enabled"])
            self.assertFalse(self.client.commands)

    def test_new_head_is_detected_after_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            monitor.save(path, self.state)
            self.state = monitor.read(path)
            self.update()
            monitor.save(path, self.state)
            self.state = monitor.read(path)
            self.tick()
            self.assertEqual(len(self.client.commands), 1)


class PollingTests(unittest.TestCase):
    def test_all_pages_and_edited_comments_exclude_own_and_pending_reviews(self):
        def gh(command, **kwargs):
            endpoint = command[4]
            item = {"id": 5, "user": {"login": "someone"}, "created_at": "2020-01-01T00:00:00Z", "updated_at": STAMP}
            if endpoint == "user":
                data = {"login": "me"}
            elif endpoint.endswith("/pulls/42"):
                data = {"head": {"sha": "head"}, "state": "open"}
            else:
                self.assertEqual(command[-2:], ["--paginate", "--slurp"])
                data = [[dict(item, user={"login": "me"}), dict(item, id=6, state="PENDING")], [item]]
            return subprocess.CompletedProcess(command, 0, json.dumps(data), "")
        with patch.object(monitor.subprocess, "run", side_effect=gh):
            result = monitor.GitHub("github.example").poll(state())
        self.assertEqual(len(result["feedback"]), 3)
        self.assertEqual(set(result["feedback"]), {"comment 5", "review comment 5", "review 5"})

    def test_api_failure_does_not_advance_the_persisted_cursor(self):
        original = state()
        with patch.object(monitor.GitHub, "get", side_effect=[{"login": "me"}, RuntimeError("temporary failure")]):
            with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                monitor.GitHub("github.example").poll(original)
        self.assertEqual(original, state())

    def test_cron_install_preserves_existing_jobs_and_is_idempotent(self):
        current = "3 * * * * refresh-credentials\n"
        def crontab(command, **kwargs):
            nonlocal current
            if command == ["crontab", "-l"]:
                return subprocess.CompletedProcess(command, 0, current, "")
            current = kwargs["input"]
        with patch.object(monitor.subprocess, "run", side_effect=crontab):
            monitor.install_cron(Path("/tmp/test resources/t3-monitors"))
            monitor.install_cron(Path("/tmp/test resources/t3-monitors"))
        self.assertEqual(len(current.splitlines()), 2)
        self.assertTrue(current.startswith("3 * * * * refresh-credentials\n"))

    def test_reopening_preserves_pending_changes_and_the_head_cursor(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(monitor, "ensure"), patch.object(monitor.shutil, "which", return_value="/custom/bin/gh"):
            root = Path(tmp)
            payload = {"prUrl": state()["prUrl"], "headSha": "old-head",
                "worktreePath": state()["worktreePath"]}
            path = monitor.register(root, Path("/t3"), payload, state()["threadId"], STAMP)
            saved = monitor.read(path)
            monitor.observe(saved, {"head": "new-head", "feedback": {}, "closed": None})
            monitor.save(path, saved)
            monitor.register(root, Path("/t3"), dict(payload, headSha="third-head"), state()["threadId"], "later")
            saved = monitor.read(path)
            self.assertEqual(saved["head"], "new-head")
            self.assertEqual(saved["since"], STAMP)
            self.assertTrue(saved["pending"])
            self.assertEqual(saved["ghPath"], "/custom/bin/gh")

    def test_saved_github_binary_works_without_a_login_shell_path(self):
        with patch.object(monitor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"login":"me"}', "")) as run:
            monitor.GitHub("github.example", "/custom/bin/gh")
        self.assertEqual(run.call_args.args[0][0], "/custom/bin/gh")


if __name__ == "__main__":
    unittest.main()
