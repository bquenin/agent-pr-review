#!/usr/bin/env python3
"""Build an unpacked extension for the locally configured GitHub hosts."""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from review_config import load


def build(destination, config):
    source = Path(__file__).resolve().parents[1] / "extension"
    destination = destination.expanduser().resolve()
    if destination == source or source in destination.parents:
        raise ValueError("Build outside the source extension directory")
    shutil.copytree(source, destination, dirs_exist_ok=True)
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["host_permissions"] = [f"https://{host}/*" for host in config["github_hosts"]]
    # Match all pages so navigation from an issue/repository to a PR works too.
    manifest["content_scripts"][0]["matches"] = manifest["host_permissions"]
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (destination / "defaults.js").write_text("const DEFAULT_REVIEW_CLI = " + json.dumps(config["default_cli"]) + ";\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path.home() / ".local/share/agent-pr-review/extension")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        print(build(args.output, load(args.config)))
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
