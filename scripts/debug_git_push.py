#!/usr/bin/env python3
"""One-off git push diagnostics for debug session 38d0d1."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

LOG_PATH = Path("/home/ejiaka/UNJOBS/.cursor/debug-38d0d1.log")
SESSION_ID = "38d0d1"
RUN_ID = "post-fix"


def log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    """Append one NDJSON debug line."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sessionId": SESSION_ID,
        "runId": RUN_ID,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run command and return exit code, stdout, stderr."""
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> None:
    """Collect evidence for git push failure hypotheses."""
    repo = Path("/home/ejiaka/UNJOBS")

    # region agent log
    code, out, err = run(["git", "remote", "-v"], repo)
    remotes = out or err
    log("H2", "debug_git_push.py:remote", "git remote -v", {"exit": code, "output": remotes})
    # endregion

    origin_url = ""
    for line in (out or "").splitlines():
        if line.startswith("origin") and "(push)" in line:
            origin_url = line.split()[1]
            break

    parsed = urlparse(origin_url.replace("git@github.com:", "https://github.com/"))
    owner_repo = parsed.path.strip("/").replace(".git", "")
    # region agent log
    log(
        "H2",
        "debug_git_push.py:parse",
        "parsed origin",
        {"origin_url": origin_url, "owner_repo": owner_repo},
    )
    # endregion

    # region agent log
    code, out, err = run(
        ["git", "ls-remote", "origin"],
        repo,
    )
    log(
        "H1",
        "debug_git_push.py:ls-remote",
        "git ls-remote origin",
        {"exit": code, "stdout": out[:500], "stderr": err[:500]},
    )
    # endregion

    # region agent log
    code, out, err = run(
        ["ssh", "-o", "BatchMode=yes", "-T", "git@github.com"],
        repo,
    )
    log(
        "H3",
        "debug_git_push.py:ssh",
        "ssh -T git@github.com",
        {"exit": code, "stdout": out[:300], "stderr": err[:300]},
    )
    # endregion

    candidates = [
        "https://github.com/Ejihand/UNJOBS",
        "https://github.com/Ejihand/UNIJOBS",
        "https://github.com/Ejihand/unjobs",
    ]
    import urllib.request

    statuses = {}
    for url in candidates:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as resp:
                statuses[url] = resp.status
        except Exception as exc:
            statuses[url] = str(exc)[:120]
    # region agent log
    log("H1", "debug_git_push.py:http", "github repo HEAD checks", {"statuses": statuses})
    # endregion


if __name__ == "__main__":
    main()
