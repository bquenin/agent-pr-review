#!/usr/bin/env python3
"""One-shot, read-only Chrome native messaging bridge to the configured Linux runtime."""
import json
from pathlib import Path
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from review_config import load, parse_pr
from review_transport import command, restore_host_path

STATES = {"missing", "exists", "running", "archived", "error"}


def query(message):
    if not isinstance(message, dict) or message.get("type") != "review-status":
        raise ValueError("Unsupported request")
    url = message.get("prUrl")
    config = load()
    parts = parse_pr(url, config)
    if url != "https://" + "/".join((*parts[:3], "pull", parts[3])):
        raise ValueError("Expected a canonical PR URL")
    # Web-page-controlled text travels only as JSON on stdin for both transports.
    result = subprocess.run(command(config, status=True),
        input=json.dumps({"prUrl": url}), text=True, capture_output=True, timeout=20, check=True)
    response = json.loads(result.stdout)
    if not isinstance(response, dict) or response.get("state") not in STATES:
        raise ValueError("Invalid status response")
    return {"state": response["state"]}


def read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise ValueError("Truncated native message")
        chunks.extend(chunk)
    return bytes(chunks)


def serve(source, target):
    try:
        size = struct.unpack("=I", read_exact(source, 4))[0]
        if not 0 < size <= 8192:
            raise ValueError("Invalid native message length")
        response = query(json.loads(read_exact(source, size)))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # Never disclose transport diagnostics, credentials or private T3 data to a page.
        response = {"state": "unavailable"}
    encoded = json.dumps(response).encode()
    target.write(struct.pack("=I", len(encoded)) + encoded)
    target.flush()


if __name__ == "__main__":
    restore_host_path()
    serve(sys.stdin.buffer, sys.stdout.buffer)
