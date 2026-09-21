#!/usr/bin/env python3
"""Validate a browser review request and dispatch to the configured runtime."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from review_config import load
from review_transport import command, restore_host_path, review_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("url")
    args = parser.parse_args()
    restore_host_path()
    config = load()
    url, cli = review_request(args.url, config)
    argv = command(config, url=url, cli=cli)
    if args.describe:
        print(json.dumps({"prUrl": url, "cli": cli}))
    else:
        os.execvp(argv[0], argv)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"agent-pr-review: {error}", file=sys.stderr)
        sys.exit(1)
