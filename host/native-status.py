#!/usr/bin/env python3
"""One-shot, read-only Chrome native messaging bridge to the configured dev host."""
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from review_config import load, parse_pr

STATES = {"missing", "exists", "running", "archived", "error"}


def query(message):
    if not isinstance(message, dict) or message.get("type") != "review-status":
        raise ValueError("Unsupported request")
    url = message.get("prUrl")
    config = load()
    parts = parse_pr(url, config)
    if url != "https://" + "/".join((*parts[:3], "pull", parts[3])):
        raise ValueError("Expected a canonical PR URL")
    host = config["ssh_host"]
    if not host:
        raise ValueError("Configure ssh_host before using the browser bridge")
    # No URL or other web-page-controlled text goes into the remote shell command.
    result = subprocess.run(["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "ClearAllForwardings=yes", "--", host,
        "python3 .local/share/agent-pr-review/t3-review.py --status"],
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
        # Never disclose SSH diagnostics, credentials or private T3 data to a page.
        response = {"state": "unavailable"}
    encoded = json.dumps(response).encode()
    target.write(struct.pack("=I", len(encoded)) + encoded)
    target.flush()


if __name__ == "__main__":
    serve(sys.stdin.buffer, sys.stdout.buffer)
