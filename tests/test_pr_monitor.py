import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MonitorTests(unittest.TestCase):
    def test_feedback_on_later_page_wakes_once_for_each_github_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            gh = directory / "gh"
            gh.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['MONITOR_CALLS'], 'a') as calls:
    calls.write(json.dumps(args) + '\\n')
if 'user' in args:
    print('reviewer')
elif '--include' in args:
    print('HTTP/2.0 200 OK\\nETag: "test"\\n')
    print(json.dumps({'head': {'sha': 'abc'}, 'state': 'open', 'merged_at': None}))
elif any('/reviews?' in arg for arg in args):
    old = [{'id': i, 'user': {'login': 'author'}, 'submitted_at': '2026-01-01T00:00:00Z', 'state': 'COMMENTED'} for i in range(100)]
    new = {'id': 101, 'user': {'login': 'author'}, 'submitted_at': '2026-02-01T00:00:00Z', 'state': 'COMMENTED'}
    print(json.dumps([old, [new]] if '--paginate' in args and '--slurp' in args else old))
else:
    print('[[]]')
''')
            gh.chmod(0o755)
            for host in ("github.com", "github.example.com", "octocorp.ghe.com"):
                calls = directory / "calls.jsonl"
                calls.write_text("")
                env = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"], MONITOR_CALLS=str(calls))
                result = subprocess.run(["bash", str(ROOT / "vm/pr-monitor.sh"), "--host", host,
                    "--repo", "team/repo", "--pr", "42", "--since", "2026-01-31T00:00:00Z", "--once"],
                    env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "PR #42 NEW REVIEW (COMMENTED) from author at 2026-02-01T00:00:00Z")
                requests = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertTrue(all(args[args.index("--hostname") + 1] == host for args in requests))
