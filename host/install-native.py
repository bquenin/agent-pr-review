#!/usr/bin/env python3
"""Install the Chrome status bridge for an unpacked extension directory on macOS."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import shutil


def extension_id(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    # Chrome derives unpacked IDs from the public key, or the absolute path.
    value = base64.b64decode(manifest["key"]) if manifest.get("key") else str(directory.resolve()).encode()
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(value).hexdigest()[:32])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-dir", type=Path, default=Path(__file__).resolve().parents[1] / "extension")
    args = parser.parse_args()
    directory = args.extension_dir.expanduser().resolve()
    eid = extension_id(directory)
    native_dir = Path.home() / "Library/Application Support/AgentPRReview/native"
    native_dir.mkdir(parents=True, exist_ok=True)
    for name in ("native-status.py", "launch-review.py"):
        target = native_dir / name
        source = Path(__file__).with_name(name).read_text().split("\n", 1)[1]
        target.write_text(f"#!{sys.executable}\n" + source)
        target.chmod(0o755)
    for name in ("review_config.py", "review_transport.py"):
        shutil.copyfile(Path(__file__).resolve().parents[1] / "lib" / name, native_dir / name)
    (native_dir / "host-path.json").write_text(json.dumps(os.environ.get("PATH", os.defpath)) + "\n")
    host = native_dir / "native-status.py"
    manifests = Path.home() / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = manifests / "com.agent_pr_review.status.json"
    manifest.write_text(json.dumps({"name": "com.agent_pr_review.status",
        "description": "Read-only T3 PR review status", "path": str(host),
        "type": "stdio", "allowed_origins": [f"chrome-extension://{eid}/"]}, indent=2) + "\n")
    print(f"Installed status bridge for {directory} (extension {eid})")
    print("Reload this extension in chrome://extensions, then refresh any open PR tabs.")


if __name__ == "__main__":
    main()
