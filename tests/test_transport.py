"""Exercise transport boundaries without SSH accounts or a container daemon."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from review_config import DEFAULTS
import review_transport as transport

ROOT = Path(__file__).resolve().parents[1]
HOSTS = ["github.com", "github.example.com", "octocorp.ghe.com"]


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="review ' workspace ")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = dict(DEFAULTS, github_hosts=HOSTS, ssh_host="dev-host",
                           devcontainer_workspace=str(self.home))

    def test_both_transports_preserve_hosts_and_runtime_arguments(self):
        binary = self.home / ".local/bin/agent-pr-review"
        binary.parent.mkdir(parents=True)
        binary.write_text(f"#!{sys.executable}\nimport json,sys; print(json.dumps(sys.argv[1:]))\n")
        binary.chmod(0o755)
        for method in ("ssh", "devcontainer"):
            self.config["transport"] = method
            for host in HOSTS:
                for cli in ("agent", "claude", "t3code"):
                    url = f"agent-pr-review://{host}/Team/Repo/pull/42?cli={cli}"
                    with self.subTest(method=method, host=host, cli=cli), \
                            patch.object(transport.shutil, "which", return_value="/tools/devcontainer"):
                        argv = transport.command(self.config, url=url)
                        if method == "ssh":
                            self.assertIn("-T" if cli == "t3code" else "-t", argv)
                            self.assertEqual(argv[-2], "dev-host")
                            runtime = shlex.split(argv[-1])
                        else:
                            self.assertEqual(argv[:5], ["/tools/devcontainer", "exec", "--workspace-folder", str(self.home), "--"])
                            runtime = argv[5:]
                        result = subprocess.run(runtime, env=dict(os.environ, HOME=str(self.home)),
                                                capture_output=True, text=True, check=True)
                        self.assertEqual(json.loads(result.stdout),
                                         [f"https://{host}/team/repo/pull/42", "--cli", cli])

    def test_status_uses_fixed_script_and_preserves_json_stdin(self):
        script = self.home / ".local/share/agent-pr-review/t3-review.py"
        script.parent.mkdir(parents=True)
        script.write_text('import json,sys; print(json.dumps({"argv":sys.argv[1:], "payload":json.load(sys.stdin)}))\n')
        for method in ("ssh", "devcontainer"):
            self.config["transport"] = method
            with patch.object(transport.shutil, "which", return_value="/tools/devcontainer"):
                argv = transport.command(self.config, status=True)
            runtime = shlex.split(argv[-1]) if method == "ssh" else argv[5:]
            result = subprocess.run(runtime, input='{"prUrl":"https://github.com/team/repo/pull/42"}',
                                    env=dict(os.environ, HOME=str(self.home)), capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), {"argv": ["--status"],
                             "payload": {"prUrl": "https://github.com/team/repo/pull/42"}})
            self.assertNotIn("up", argv)

    def test_missing_destination_or_cli_has_actionable_error(self):
        self.config.update(ssh_host="")
        with self.assertRaisesRegex(ValueError, "ssh_host"):
            transport.command(self.config, status=True)
        self.config.update(transport="devcontainer", devcontainer_workspace="")
        with self.assertRaisesRegex(ValueError, "devcontainer_workspace"):
            transport.command(self.config, status=True)
        self.config["devcontainer_workspace"] = str(self.home)
        with patch.object(transport.shutil, "which", return_value=None), \
                self.assertRaisesRegex(ValueError, "Dev Container CLI not found"):
            transport.command(self.config, status=True)

    def test_untrusted_url_or_backend_never_reaches_runtime(self):
        for url in ("https://evil.example/t/r/pull/42", "https://github.com/t/r/pull/42;id",
                    "agent-pr-review://github.com/t/r/pull/42?cli=$(id)",
                    "agent-pr-review://github.com/t/r/pull/42?cli="):
            with self.subTest(url=url), self.assertRaises(ValueError):
                transport.command(self.config, url=url)

    def test_installed_launcher_works_with_sparse_gui_path(self):
        extension = self.home / "extension"
        extension.mkdir()
        (extension / "manifest.json").write_text('{}')
        cli_dir = self.home / "node-bin"
        cli_dir.mkdir()
        stub = cli_dir / "devcontainer"
        stub.write_text(f"#!{sys.executable}\nimport json,sys; print(json.dumps(sys.argv[1:]))\n")
        stub.chmod(0o755)
        config_path = self.home / ".config/agent-pr-review/config.json"
        config_path.parent.mkdir(parents=True)
        self.config["transport"] = "devcontainer"
        config_path.write_text(json.dumps(self.config))
        env = dict(os.environ, HOME=str(self.home), PATH=str(cli_dir) + os.pathsep + os.environ["PATH"])
        subprocess.run([sys.executable, str(ROOT / "host/install-native.py"), "--extension-dir", str(extension)],
                       env=env, capture_output=True, text=True, check=True)
        installed = self.home / "Library/Application Support/AgentPRReview/native/launch-review.py"
        # Launch Services starts the installed helper, with no source checkout on sys.path.
        env["PATH"] = "/usr/bin:/bin"
        result = subprocess.run([str(installed), "agent-pr-review://octocorp.ghe.com/t/r/pull/42?cli=claude"],
                                env=env, cwd=self.home, capture_output=True, text=True, check=True)
        args = json.loads(result.stdout)
        self.assertEqual(args[:4], ["exec", "--workspace-folder", str(self.home), "--"])
        self.assertEqual(args[-3:], ["https://octocorp.ghe.com/t/r/pull/42", "--cli", "claude"])
