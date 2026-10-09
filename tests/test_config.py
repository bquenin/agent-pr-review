import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import review_config as config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        home = patch.object(config.Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.path = config.config_path()
        self.path.parent.mkdir(parents=True)

    def configure(self, value):
        self.path.write_text(json.dumps(value))
        return config.load()

    def test_defaults_and_legacy_ssh_alias(self):
        self.assertEqual(config.load()["github_hosts"], ["github.com"])
        self.assertFalse(config.load()["trust_worktrees"])
        (self.path.parent / "ssh-host").write_text("\nmy-dev-host\n")
        self.assertEqual(config.load()["ssh_host"], "my-dev-host")
        self.assertEqual(self.configure({"ssh_host": "new-dev-host"})["ssh_host"], "new-dev-host")

    def test_bad_configuration_is_rejected(self):
        for invalid in ([], {"github_hosts": []}, {"github_hosts": ["*.ghe.com"]},
                {"github_hosts": ["https://github.com"]}, {"github_hosts": ["evil.example:443"]},
                {"monitor": "true"}, {"monitor": True}, {"ssh_host": "-oProxyCommand=bad"},
                {"transport": "cloud-workspace"}, {"devcontainer_workspace": "relative"},
                {"devcontainer_command": "-x"}, {"devcontainer_command": "relative/bin/cli"},
                {"tmux_control_mode": "true"}, {"typo": True}, {"legacy_host": "unknown.example"}, {"repo_roots": ["relative"]},
                {"host_instructions": {"unknown.example": "text"}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.configure(invalid)

    def test_clone_and_auto_review_settings(self):
        hosts = ["github.com", "github.example.com"]
        settings = self.configure({"github_hosts": hosts, "repo_roots": ["~/code"], "clone_root": "~/code/clones",
            "clone_protocol": "https", "default_cli": "t3code", "auto_review_hosts": ["GitHub.Example.com"]})
        self.assertEqual(settings["auto_review_hosts"], ["github.example.com"])
        self.assertEqual(config.load()["auto_review_hosts"], ["github.example.com"])
        self.assertEqual(self.configure({"clone_root": "~/code"})["clone_protocol"], "ssh")
        for invalid in ({"clone_root": "relative/clones"}, {"clone_root": "~/elsewhere"},
                {"clone_root": "~/code-other"}, {"clone_root": "~/code/a/b/c"}, {"clone_protocol": "git"},
                {"auto_review_hosts": "github.com", "default_cli": "t3code"},
                {"auto_review_hosts": [1], "default_cli": "t3code"},
                {"auto_review_hosts": ["unknown.example"], "default_cli": "t3code"},
                {"auto_review_hosts": ["github.com"]}, {"auto_review_hosts": ["github.com"], "default_cli": "claude"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.configure(invalid)

    def test_timeout_kills_the_whole_process_group_and_keeps_partial_output(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            # A surviving grandchild would hold stdout open for 30 seconds.
            config.run_with_timeout(["sh", "-c", "echo started; sleep 30 & wait"], 0.5,
                stdout=subprocess.PIPE, text=True)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(raised.exception.output, "started\n")

    def test_concurrent_clone_never_removes_the_other_launchs_checkout(self):
        home = self.home.resolve()
        env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}
        def git(*args):
            subprocess.run(["git", *args], check=True, capture_output=True, env={**config.os.environ, **env})
        seed, bare = home / "seed", home / "upstream.git"
        git("init", "-b", "main", str(seed))
        git("-C", str(seed), "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-m", "base")
        git("clone", "--bare", str(seed), str(bare))
        settings = self.configure({"github_hosts": ["github.com", "github.example.com"],
            "repo_roots": [str(home / "code")], "clone_root": str(home / "code/clones"), "clone_protocol": "https"})
        run = config.run_with_timeout
        def real_run(command, timeout, **kwargs):
            return run(command, timeout, **{**kwargs, "stderr": subprocess.DEVNULL})
        for outcome, repo in (("succeeds", "first"), ("fails", "second")):
            expected = f"github.example.com/team/{repo}"
            git("config", "--global", "--add", f"url.{bare.as_uri()}.insteadOf", f"https://{expected}.git")
            target = home / "code/clones/team" / repo
            def racing(command, *args, **kwargs):
                # The other launch (a click, or the poller) finishes first while this
                # clone is still running, and the user starts working in its checkout.
                with patch.object(config, "run_with_timeout", real_run):
                    self.assertEqual(config.clone_repo(settings, expected), target)
                (target / "notes.txt").write_text("uncommitted\n")
                if outcome == "succeeds":
                    return real_run(command, *args, **kwargs)
                Path(command[-1]).mkdir()
                (Path(command[-1]) / "partial").write_text("")
                return subprocess.CompletedProcess(command, 128)
            with self.subTest(outcome=outcome), open(config.os.devnull, "w") as quiet, \
                    patch.dict(config.os.environ, env), patch.object(config.sys, "stderr", quiet), \
                    patch.object(config, "run_with_timeout", side_effect=racing):
                if outcome == "succeeds":
                    self.assertEqual(config.clone_repo(settings, expected), target)
                else:
                    with self.assertRaisesRegex(ValueError, "git clone of .* failed"):
                        config.clone_repo(settings, expected)
                self.assertEqual((target / "notes.txt").read_text(), "uncommitted\n")
                self.assertEqual(config.matching_remote(target, expected, {}), "origin")
                # The loser's temporary clone is gone; nothing else was touched.
                self.assertEqual({p.name for p in target.parent.iterdir()}, {"first", repo})

    def test_url_normalization_and_enterprise_hosts(self):
        settings = self.configure({"github_hosts": ["github.com", "github.example.com", "octocorp.ghe.com"]})
        for host in settings["github_hosts"]:
            for suffix in ("", "/files", "/commits", "/checks", "?cli=claude#discussion"):
                self.assertEqual(config.parse_pr(f"https://{host}/Team/Repo/pull/42{suffix}", settings),
                    (host, "team", "repo", "42"))

    def test_dot_prefixed_repository_names(self):
        self.assertEqual(config.parse_pr("https://github.com/team/.github/pull/1", config.load()),
            ("github.com", "team", ".github", "1"))

    def test_url_rejects_shell_input_credentials_paths_and_unconfigured_hosts(self):
        for url in ("http://github.com/t/r/pull/1", "https://evil.example/t/r/pull/1",
                "https://user@github.com/t/r/pull/1", "https://github.com:443/t/r/pull/1",
                "https://github.com/t/../pull/1", "https://github.com/t/%72/pull/1",
                "https://github.com/t/r/pull/0", "https://github.com/t/r/pull/-1",
                "https://github.com/t/r/pull/1;touch", "https://github.com/t/r/pull/1\n",
                "https://github.com/t/repo$(id)/pull/1", "https://github.com/t/r/pull/1garbage"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                config.parse_pr(url, config.load())

    def test_remote_identity_supports_https_ssh_and_aliases(self):
        for value in ("https://github.com/Team/Repo.git", "ssh://git@github.com/Team/Repo.git",
                "git@github.com:Team/Repo.git", "git@work-alias:Team/Repo.git"):
            self.assertEqual(config.remote_identity(value, {"work-alias": "github.com"}), "github.com/team/repo")

    def test_extension_build_contains_only_configured_hosts_and_no_private_instructions(self):
        spec = importlib.util.spec_from_file_location("build_extension", Path(__file__).resolve().parents[1] / "tools/build-extension.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        settings = self.configure({"github_hosts": ["github.com", "github.example.com", "octocorp.ghe.com"],
            "default_cli": "t3code", "host_instructions": {"github.example.com": "private instruction"}})
        out = builder.build(self.home / "extension", settings)
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["host_permissions"], [f"https://{host}/*" for host in settings["github_hosts"]])
        self.assertEqual(manifest["content_scripts"][0]["matches"], manifest["host_permissions"])
        self.assertIn('"t3code"', (out / "defaults.js").read_text())
        self.assertNotIn("private instruction", "".join(p.read_text() for p in out.glob("*.*") if p.suffix != ".png"))
