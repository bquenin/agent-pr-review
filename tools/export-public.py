#!/usr/bin/env python3
"""Export a clean committed snapshot without Git history or local private state."""
import argparse
from pathlib import Path
import shutil
import subprocess


def export(source, destination):
    source, destination = source.resolve(), destination.expanduser().resolve()
    if destination.exists():
        raise ValueError("Destination already exists; choose a new empty path")
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True)
    if dirty:
        raise ValueError("Commit and review the source snapshot before exporting")
    files = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"]).decode().split("\0")
    for name in filter(None, files):
        original = source / name
        if original.is_symlink() or not original.is_file():
            raise ValueError(f"Unsupported export entry: {name}")
    destination.mkdir(parents=True)
    for name in filter(None, files):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
        target.chmod((source / name).stat().st_mode & 0o777)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(export(Path(__file__).resolve().parents[1], args.destination))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")
