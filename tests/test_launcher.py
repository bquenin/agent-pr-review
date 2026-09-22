"""Exercise the real launcher and Git; redirect test HTTPS remotes to local bare repos."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOSTS = ["github.com", "github.example.com", "octocorp.ghe.com"]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(self.home / ".gitconfig"),
            XDG_CONFIG_HOME=str(self.home / "custom-xdg"),
            AGENT_PR_REVIEW_RESOURCES=str(self.home / "resources"))
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT"):
            self.env.pop(key, None)
        self.settings = {"github_hosts": HOSTS, "repo_roots": [str(self.home / "code")],
            "default_cli": "claude"}
        self.config = self.home / ".config/agent-pr-review/config.json"
        self.config.parent.mkdir(parents=True)
        self.save_config()
        resources = self.home / "resources"
        resources.mkdir()
        for name in ("review-methodology.md", "review-posting-rules.md"):
            shutil.copyfile(ROOT / "prompts" / name, resources / name)
        shutil.copyfile(ROOT / "lib/review_config.py", resources / "review_config.py")
        self.git("config", "--global", "user.email", "test@example.com")
        self.git("config", "--global", "user.name", "Test")
        seed = self.home / "seed"
        self.git("init", "-b", "main", str(seed))
        (seed / "file.txt").write_text("base\n")
        self.git("-C", str(seed), "add", ".")
        self.git("-C", str(seed), "commit", "-m", "base")
        self.bare = self.home / "upstream.git"
        self.git("clone", "--bare", str(seed), str(self.bare))
        self.git("-C", str(self.bare), "update-ref", "refs/pull/42/head", "HEAD")
        self.repos = {}
        for host in HOSTS:
            repo = self.home / "code" / host / "renamed-clone"
            repo.parent.mkdir(parents=True)
            self.git("clone", str(self.bare), str(repo))
            url = f"https://{host}/team/repo.git"
            self.git("-C", str(repo), "remote", "set-url", "origin", url)
            self.git("config", "--global", "--add", f"url.{self.bare.as_uri()}.insteadOf", url)
            self.repos[host] = repo

    def git(self, *args):
        return subprocess.run(["git", *args], env=self.env, check=True, capture_output=True, text=True).stdout.strip()

    def save_config(self):
        self.config.write_text(json.dumps(self.settings))

    def launch(self, host="github.com", *args, ok=True):
        result = subprocess.run(["bash", str(ROOT / "runtime/agent-pr-review"), "--print-cmd", "--no-tmux",
            f"https://{host}/team/repo/pull/42", *args], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def worktree(self, result):
        return Path(re.search(r"^launch-dir: (.+)$", result.stdout, re.M)[1])

    def test_real_worktrees_and_sessions_are_distinct_across_three_hosts(self):
        ids, paths = set(), set()
        for host in HOSTS:
            result = self.launch(host)
            paths.add(self.worktree(result))
            ids.add(re.search(r"Starting Claude session: (.+)", result.stdout)[1])
            self.assertIn(f"gh api --hostname {host}", result.stdout)
            self.assertEqual(self.git("-C", str(self.repos[host]), "branch", "--show-current"), "main")
            self.assertEqual(self.git("-C", str(self.repos[host]), "status", "--porcelain"), "")
            self.assertIn("Do not post comments", result.stdout)
            self.assertNotIn("--approve", result.stdout)
            self.assertFalse((self.home / ".claude.json").exists())
            self.assertEqual(self.worktree(self.launch(host)), self.worktree(result))
        self.assertEqual(len(ids), 3)
        self.assertEqual(len({p.name for p in paths}), 3)

    def test_models_policy_instructions_and_cursor_trust_are_configurable(self):
        self.settings.update(default_cli="agent", cursor_model="test-model", trust_worktrees=True,
            posting_policy="approve", monitor=True,
            host_instructions={"github.example.com": "Use the organization's locally installed skill."})
        self.save_config()
        result = self.launch("github.example.com")
        self.assertIn("'--model' 'test-model' '--trust'", result.stdout)
        self.assertIn("Use the organization", result.stdout)
        self.assertIn("--approve", result.stdout)
        self.assertIn("PR monitor", result.stdout)
        self.assertNotIn("Use the organization", self.launch().stdout)

    def test_t3_cleanup_stops_watcher_before_removing_worktree(self):
        self.settings.update(default_cli="t3code", monitor=True, posting_policy="comment")
        self.save_config()
        for name, content in (("t3-review.py", "import sys; print(sys.stdin.read())\n"),
                ("t3_monitor.py", "")):
            helper = self.home / "resources" / name
            helper.write_text("#!/usr/bin/env python3\n" + content)
            helper.chmod(0o755)
        result = self.launch()
        self.assertIn("--stop-worktree", result.stdout)
        self.assertIn(".agent-pr-review/worktrees/pr-42", result.stdout)
        self.assertLess(result.stdout.index("--stop-worktree"), result.stdout.index("worktree remove"))

    def test_ambiguity_requires_explicit_repo_and_wrong_remote_is_rejected(self):
        other = self.home / "code/duplicate"
        self.git("clone", str(self.bare), str(other))
        self.git("-C", str(other), "remote", "set-url", "origin", "https://github.com/team/repo.git")
        self.assertIn("Use --repo", self.launch(ok=False).stderr)
        self.launch("github.com", "--repo", str(other))
        self.assertIn("No fetch remote", self.launch("github.com", "--repo", str(self.repos[HOSTS[1]]), ok=False).stderr)

    def test_dirty_worktree_and_fetch_failure_never_launch_canonical_checkout(self):
        worktree = self.worktree(self.launch())
        (worktree / "file.txt").write_text("keep my changes\n")
        self.assertIn("local changes", self.launch(ok=False).stderr)
        self.assertEqual((worktree / "file.txt").read_text(), "keep my changes\n")
        self.git("-C", str(self.bare), "update-ref", "-d", "refs/pull/42/head")
        result = self.launch(ok=False)
        self.assertIn("cannot prepare an isolated", result.stderr)
        self.assertNotIn("command:", result.stdout)
        self.assertEqual((self.repos[HOSTS[0]] / "file.txt").read_text(), "base\n")

    def test_legacy_host_resumes_existing_worktree_and_claude_session(self):
        host = HOSTS[1]
        repo = self.repos[host]
        legacy = repo / ".agent-pr-review/worktrees/pr-42"
        self.git("-C", str(repo), "worktree", "add", "-b", "agent-pr-review/pr-42", str(legacy), "HEAD")
        session = str(uuid.uuid5(uuid.NAMESPACE_URL, "team/repo/pull/42"))
        project = re.sub(r"[^A-Za-z0-9]", "-", str(legacy))
        transcript = self.home / ".claude/projects" / project / (session + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{}\n')
        self.settings["legacy_host"] = host
        self.save_config()
        result = self.launch(host)
        self.assertEqual(self.worktree(result), legacy)
        self.assertIn("Resuming Claude session: " + session, result.stdout)
        self.assertNotIn(session, self.launch().stdout)

    def test_explicit_cli_wins_over_url_query(self):
        result = subprocess.run(["bash", str(ROOT / "runtime/agent-pr-review"), "--print-cmd", "--no-tmux", "--cli", "agent",
            "https://github.com/team/repo/pull/42/files?cli=claude"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cli: agent", result.stdout)
        self.assertNotIn("--trust", result.stdout)

    def test_cursor_legacy_mapping_is_copied_only_for_designated_host(self):
        self.settings.update(default_cli="agent", legacy_host=HOSTS[1])
        self.save_config()
        mapping = self.home / "resources/cursor-sessions.json"
        mapping.write_text(json.dumps({"team/repo/pull/42": "legacy-chat-id"}))
        # A stub agent records the launch without making a model call.
        binary = self.home / "bin/agent"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        binary.chmod(0o755)
        result = subprocess.run(["bash", str(ROOT / "runtime/agent-pr-review"), "--no-tmux",
            f"https://{HOSTS[1]}/team/repo/pull/42"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("legacy-chat-id", result.stdout)
        self.assertEqual(json.loads(mapping.read_text())[f"{HOSTS[1]}/team/repo/pull/42"], "legacy-chat-id")
        self.assertNotIn("legacy-chat-id", self.launch().stdout)

    def test_standard_tmux_attachment_and_explicit_iterm_mode(self):
        binary = self.home / "bin/tmux"
        binary.parent.mkdir(exist_ok=True)
        binary.write_text("""#!/bin/sh
case "$1" in
    new-window) printf '@1\\n' ;;
    attach|-CC) printf 'attach argv: %s\\n' "$*" ;;
esac
""")
        binary.chmod(0o755)
        self.env.pop("TMUX", None)
        for control in (False, True):
            self.settings["tmux_control_mode"] = control
            self.save_config()
            result = subprocess.run(["bash", str(ROOT / "runtime/agent-pr-review"),
                "https://github.com/team/repo/pull/42"], env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            expected = "-CC attach -t agents" if control else "attach -t agents"
            self.assertIn("attach argv: " + expected, result.stdout)
