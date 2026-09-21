#!/usr/bin/env python3
"""Install the Chrome status bridge for an unpacked extension directory on macOS."""
import argparse
import base64
import hashlib
import json
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
    host = native_dir / "native-status.py"
    source = Path(__file__).with_name("native-status.py").read_text().split("\n", 1)[1]
    host.write_text(f"#!{sys.executable}\n" + source)
    host.chmod(0o755)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "lib/review_config.py", native_dir / "review_config.py")
    manifests = Path.home() / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = manifests / "com.agent_pr_review.status.json"
    manifest.write_text(json.dumps({"name": "com.agent_pr_review.status",
        "description": "Read-only T3 PR review status over SSH", "path": str(host),
        "type": "stdio", "allowed_origins": [f"chrome-extension://{eid}/"]}, indent=2) + "\n")
    print(f"Installed status bridge for {directory} (extension {eid})")
    print("Reload this extension in chrome://extensions, then refresh any open PR tabs.")


if __name__ == "__main__":
    main()
