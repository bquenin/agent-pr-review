import importlib.util
import io
import json
from pathlib import Path
import struct
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("native_status", Path(__file__).parents[1] / "host/native-status.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
REQUEST = {"type": "review-status", "prUrl": "https://github.example.com/team/repo/pull/42"}


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        home = patch.object(native.Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        config = self.home / ".config/agent-pr-review/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"github_hosts": ["github.com", "github.example.com", "octocorp.ghe.com"], "ssh_host": "dev-host"}))

    def test_all_configured_github_hosts(self):
        for host in ("github.com", "github.example.com", "octocorp.ghe.com"):
            request = dict(REQUEST, prUrl=f"https://{host}/team/repo/pull/42")
            with patch.object(native.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"state":"exists"}')) as run:
                self.assertEqual(native.query(request), {"state": "exists"})
                self.assertEqual(json.loads(run.call_args.kwargs["input"])["prUrl"], request["prUrl"])

    def test_installer_registers_only_target_extension_and_absolute_python(self):
        spec = importlib.util.spec_from_file_location("install_native", Path(__file__).parents[1] / "host/install-native.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extension = root / "extension"
            extension.mkdir()
            (extension / "manifest.json").write_text('{"version":"1.4.0"}')
            with patch.object(installer.Path, "home", return_value=root), \
                    patch.object(installer.sys, "argv", ["install-native.py", "--extension-dir", str(extension)]), \
                    patch.object(installer.sys, "stdout", io.StringIO()):
                installer.main()
            manifest = json.loads((root / "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.agent_pr_review.status.json").read_text())
            self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{installer.extension_id(extension)}/"])
            installed = Path(manifest["path"])
            self.assertTrue(installed.is_absolute())
            self.assertEqual(installed.read_text().splitlines()[0], "#!" + installer.sys.executable)

    def test_framing_and_response_allowlist(self):
        data = json.dumps(REQUEST).encode()
        result = io.BytesIO()
        with patch.object(native, "query", return_value={"state": "exists"}) as query:
            native.serve(io.BytesIO(struct.pack("=I", len(data)) + data), result)
        query.assert_called_once_with(REQUEST)
        frame = result.getvalue()
        self.assertEqual(struct.unpack("=I", frame[:4])[0], len(frame[4:]))
        self.assertEqual(json.loads(frame[4:]), {"state": "exists"})

    def test_invalid_frames_never_invoke_ssh(self):
        for frame in (b"", b"\0", struct.pack("=I", 9000), struct.pack("=I", 0), struct.pack("=I", 5) + b"{}"):
            with self.subTest(frame=frame), patch.object(native.subprocess, "run") as run:
                out = io.BytesIO()
                native.serve(io.BytesIO(frame), out)
                self.assertEqual(json.loads(out.getvalue()[4:]), {"state": "unavailable"})
                run.assert_not_called()

    def test_ssh_uses_fixed_command_json_stdin_and_configured_host(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(native.Path, "home", return_value=Path(tmp)):
            config = Path(tmp) / ".config/agent-pr-review/ssh-host"
            config.parent.mkdir(parents=True)
            config.write_text("\ndev-host\n")
            (config.parent / "config.json").write_text(json.dumps({"github_hosts": ["github.example.com"]}))
            with patch.object(native.subprocess, "run", return_value=subprocess.CompletedProcess([], 0,
                    '{"state":"exists","private":"ignored"}')) as run:
                self.assertEqual(native.query(REQUEST), {"state": "exists"})
            command = run.call_args.args[0]
            self.assertEqual(command[-2], "dev-host")
            self.assertEqual(shlex.split(command[-1]), ["/bin/sh", "-c",
                'exec python3 "$HOME/.local/share/agent-pr-review/t3-review.py" --status', "agent-pr-review"])
            self.assertNotIn(REQUEST["prUrl"], command)
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"prUrl": REQUEST["prUrl"]})
            self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_untrusted_requests_never_invoke_ssh(self):
        for message in (None, [], dict(REQUEST, type="launch"), dict(REQUEST, prUrl="https://evil.example/team/repo/pull/42"),
                dict(REQUEST, prUrl=REQUEST["prUrl"] + "'; touch /tmp/pwn"),
                dict(REQUEST, prUrl="https://github.example.com/../repo/pull/42")):
            with self.subTest(message=message), patch.object(native.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    native.query(message)
                run.assert_not_called()

    def test_transport_failure_returns_only_unavailable(self):
        data = json.dumps(REQUEST).encode()
        with patch.object(native, "query", side_effect=subprocess.TimeoutExpired("ssh", 20)):
            out = io.BytesIO()
            native.serve(io.BytesIO(struct.pack("=I", len(data)) + data), out)
        self.assertEqual(json.loads(out.getvalue()[4:]), {"state": "unavailable"})

    def test_devcontainer_status_is_read_only_and_filters_private_fields(self):
        config = self.home / ".config/agent-pr-review/config.json"
        config.write_text(json.dumps({"transport": "devcontainer", "devcontainer_workspace": str(self.home),
                                     "github_hosts": ["github.example.com"]}))
        with patch("review_transport.shutil.which", return_value="/tools/devcontainer"), \
                patch.object(native.subprocess, "run", return_value=subprocess.CompletedProcess([], 0,
                    '{"state":"running","private":"ignored"}')) as run:
            self.assertEqual(native.query(REQUEST), {"state": "running"})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ["/tools/devcontainer", "exec", "--workspace-folder", str(self.home), "--"])
        self.assertNotIn("up", argv)
        self.assertNotIn(REQUEST["prUrl"], argv)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"prUrl": REQUEST["prUrl"]})
