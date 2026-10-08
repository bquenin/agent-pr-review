"""Exercise the review-request poller against simulated gh and launcher executables."""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_requests", ROOT / "runtime/review_requests.py")
poller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poller)

HOST = "github.example.com"
OTHER = "octocorp.ghe.com"
GO_LIVE = "2026-10-01T00:00:00Z"

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
key = sys.argv[3] + " " + sys.argv[4].split("?")[0]
responses = json.load(open(os.environ["FAKE_GH_RESPONSES"]))
if key not in responses:
    sys.exit("gh: Not Found (HTTP 404)")
print(json.dumps(responses[key]))
"""
FAKE_LAUNCHER = """#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_LAUNCHES"
echo "agent-pr-review: Received URL: $3"
if [ "${FAKE_LAUNCH_EXIT:-0}" != 0 ]; then
    echo "agent-pr-review: T3 server is not running" >&2
fi
exit "${FAKE_LAUNCH_EXIT:-0}"
"""


def at(minutes):
    return f"2026-10-01T{minutes // 60:02d}:{minutes % 60:02d}:00Z"


def requested(stamp, login="reviewer"):
    return {"event": "review_requested", "created_at": stamp, "requested_reviewer": {"login": login}}


class PollTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.root = self.home / "resources/review-requests"
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        for name, script in (("gh", FAKE_GH), ("agent-pr-review", FAKE_LAUNCHER)):
            (bin_dir / name).write_text(script)
            (bin_dir / name).chmod(0o755)
        self.calls = self.home / "gh-calls"
        self.launches_file = self.home / "launches"
        self.responses_file = self.home / "responses.json"
        env = patch.dict(os.environ, {"HOME": str(self.home), "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_GH_CALLS": str(self.calls), "FAKE_GH_RESPONSES": str(self.responses_file),
            "FAKE_LAUNCHES": str(self.launches_file)})
        env.start()
        self.addCleanup(env.stop)
        self.config = self.home / "config.json"
        self.configure([HOST])
        self.logins = {HOST: "Reviewer", OTHER: "reviewer"}
        self.open = {HOST: {}, OTHER: {}}  # Search results: number -> timeline pages.
        self.unreadable = set()  # PR numbers whose timeline request fails.

    def configure(self, hosts):
        self.config.write_text(json.dumps({"github_hosts": ["github.com", HOST, OTHER],
            "default_cli": "t3code", "auto_review_hosts": hosts}))

    def request(self, number, *pages, host=HOST):
        self.open[host][number] = list(pages)

    def poll(self, minutes):
        responses = {}
        for host, prs in self.open.items():
            if self.logins.get(host):
                responses[f"{host} user"] = {"login": self.logins[host]}
            # A new review request bumps the PR's updated_at.
            responses[f"{host} search/issues"] = {"items": [{"html_url": f"https://{host}/Team/Repo/pull/{n}",
                "updated_at": max(e["created_at"] for page in pages for e in page)} for n, pages in prs.items()]}
            for number, pages in prs.items():
                if number not in self.unreadable:
                    responses[f"{host} repos/team/repo/issues/{number}/timeline"] = pages
        self.responses_file.write_text(json.dumps(responses))
        return poller.poll(self.root, self.config, at(minutes))

    def launches(self):
        return self.launches_file.read_text().splitlines() if self.launches_file.exists() else []

    def timeline_calls(self):
        return self.calls.read_text().count("/timeline")

    def state(self):
        return json.loads((self.root / "state.json").read_text())

    def log(self):
        return (self.root / "poll.log").read_text() if (self.root / "poll.log").exists() else ""

    def test_go_live_cutoff_skips_requests_made_before_the_first_poll(self):
        self.request(1, [requested("2026-09-30T23:00:00Z")])
        self.assertTrue(self.poll(0))
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.state()["startedAt"], at(0))
        self.assertIn(f"skipped https://{HOST}/team/repo/pull/1: requested before", self.log())
        before = self.log()
        self.poll(1)
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.log(), before)

    def test_new_request_launches_once_and_a_handled_request_is_skipped(self):
        self.poll(0)
        self.request(2, [requested(at(0))], [requested("2026-10-01T00:00:30Z")])
        self.poll(1)
        self.assertEqual(self.launches(), [f"--cli t3code https://{HOST}/team/repo/pull/2"])
        self.poll(2)
        self.poll(3)
        self.assertEqual(self.timeline_calls(), 1)  # An unchanged PR is not refetched every minute.
        self.poll(30)
        self.assertEqual(self.timeline_calls(), 2)
        self.assertEqual(len(self.launches()), 1)
        entry = self.state()["reviews"][f"{HOST}/team/repo/pull/2"]
        self.assertEqual((entry["result"], entry["attempts"], entry["requestedAt"]),
            ("launched", 1, "2026-10-01T00:00:30Z"))
        self.assertIn("launched", self.log())
        self.assertIn("    agent-pr-review: Received URL", self.log())

    def test_re_request_after_a_review_launches_again(self):
        self.poll(0)
        self.request(3, [requested(at(1))])
        self.poll(2)
        del self.open[HOST][3]  # The submitted review removed the request.
        self.poll(3)
        self.request(3, [requested(at(1)), requested(at(4))])
        self.poll(5)
        self.assertEqual(len(self.launches()), 2)
        self.assertEqual(self.state()["reviews"][f"{HOST}/team/repo/pull/3"]["requestedAt"], at(4))

    def test_only_direct_requests_for_this_user_are_considered(self):
        self.poll(0)
        # A team-only request never matches user-review-requested:@me.
        self.request(4, [requested(at(1)), {"event": "review_requested", "created_at": at(2),
            "requested_team": {"slug": "reviewers"}}, requested(at(3), "someone-else")])
        self.poll(5)
        self.assertEqual(self.state()["reviews"][f"{HOST}/team/repo/pull/4"]["requestedAt"], at(1))
        searches = [json.loads(line) for line in self.calls.read_text().splitlines() if "search/issues" in line]
        query = next(arg for arg in searches[0] if arg.startswith("q="))
        self.assertIn("user-review-requested:@me", query)
        self.assertNotIn("team-review-requested", query)

    def test_failed_launch_retries_with_backoff_then_gives_up_until_a_newer_request(self):
        self.poll(0)
        self.request(5, [requested(at(1))])
        with patch.dict(os.environ, FAKE_LAUNCH_EXIT="1"):
            for minute in (2, 3, 7, 8, 17, 18, 60):
                self.poll(minute)
        self.assertEqual(len(self.launches()), 3)
        entry = self.state()["reviews"][f"{HOST}/team/repo/pull/5"]
        self.assertEqual((entry["result"], entry["attempts"]), ("failed", 3))
        self.assertIn("T3 server is not running", entry["lastError"])
        self.assertIn("giving up until a newer request", self.log())
        self.request(5, [requested(at(1)), requested(at(61))])
        self.poll(62)
        self.assertEqual(len(self.launches()), 4)
        self.assertEqual(self.state()["reviews"][f"{HOST}/team/repo/pull/5"]["result"], "launched")

    def test_held_lock_makes_poll_a_quiet_no_op(self):
        self.root.mkdir(parents=True)
        with (self.root / "poll.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertFalse(self.poll(0))
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root / "state.json").exists())

    def test_a_failing_host_does_not_stop_others_and_is_logged_once(self):
        self.configure([HOST, OTHER])
        self.logins[HOST] = None  # gh api user fails on this host.
        self.poll(0)
        self.request(6, [requested(at(1))], host=OTHER)
        self.poll(2)
        self.poll(3)
        self.assertEqual(self.launches(), [f"--cli t3code https://{OTHER}/team/repo/pull/6"])
        self.assertIn(HOST, self.state()["lastError"])
        self.assertEqual(self.log().count(f"error {HOST}"), 1)

    def test_a_failing_pr_does_not_stop_the_others(self):
        self.poll(0)
        self.request(11, [requested(at(1))])
        self.request(12, [requested(at(1))])
        self.request(13, [requested(at(1))])
        self.unreadable.add(12)
        self.poll(2)
        self.assertEqual(self.launches(), [f"--cli t3code https://{HOST}/team/repo/pull/{n}" for n in (11, 13)])
        self.assertIn(f"{HOST}/team/repo/pull/12", self.state()["lastError"])

    def test_disabled_poller_does_nothing_and_invalid_config_is_recorded(self):
        self.configure([])
        self.poll(0)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root / "state.json").exists())
        self.config.write_text(json.dumps({"default_cli": "agent", "auto_review_hosts": ["github.com"]}))
        self.poll(1)
        self.poll(2)
        self.assertIn("requires default_cli t3code", self.state()["lastError"])
        self.assertEqual(self.log().count("error config"), 1)
        self.assertFalse(self.calls.exists())

    def test_entries_no_longer_requested_are_pruned_after_thirty_days(self):
        self.poll(0)
        self.request(7, [requested(at(1))])
        self.request(8, [requested(at(1))])
        self.poll(2)
        del self.open[HOST][7]
        state = self.state()
        for entry in state["reviews"].values():
            entry["seenAt"] = "2026-08-01T00:00:00Z"
        poller.save(self.root / "state.json", state)
        self.poll(3)
        self.assertEqual(list(self.state()["reviews"]), [f"{HOST}/team/repo/pull/8"])

    def test_status_lists_recent_reviews_first(self):
        self.poll(0)
        self.request(9, [requested(at(1))])
        self.poll(2)
        self.request(10, [requested(at(2))])
        self.poll(3)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            poller.status(self.root)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual((lines[0]["startedAt"], lines[0]["lastPollAt"]), (at(0), at(3)))
        self.assertEqual([line["pr"] for line in lines[1:]],
            [f"{HOST}/team/repo/pull/10", f"{HOST}/team/repo/pull/9"])


class CronTests(unittest.TestCase):
    def test_install_replaces_its_line_keeps_other_jobs_and_uninstall_removes_it(self):
        current = "3 * * * * refresh-credentials\n"
        def crontab(command, **kwargs):
            nonlocal current
            if command == ["crontab", "-l"]:
                return subprocess.CompletedProcess(command, 0, current, "")
            current = kwargs["input"]
        resources = Path("/tmp/test 100% resources")
        with patch.object(poller.subprocess, "run", side_effect=crontab), \
                patch.dict(os.environ, PATH="/opt/tools/bin:/usr/bin"):
            poller.install_cron(resources)
            poller.install_cron(resources)
            lines = current.splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], "3 * * * * refresh-credentials")
            self.assertTrue(lines[1].startswith("* * * * * env PATH=/opt/tools/bin:/usr/bin "))
            self.assertIn(r"'AGENT_PR_REVIEW_RESOURCES=/tmp/test 100\% resources'", lines[1])
            self.assertIn("review_requests.py --poll >/dev/null 2>&1 ", lines[1])
            self.assertTrue(lines[1].endswith(r"# agent-pr-review review requests /tmp/test 100\% resources"))
            self.assertNotIn(" 100% ", lines[1])
            poller.uninstall_cron(resources)
        self.assertEqual(current, "3 * * * * refresh-credentials\n")


if __name__ == "__main__":
    unittest.main()
