"""Run only on GitHub-hosted CI; preserve complete output and real exit status."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def capture(command, path):
    with path.open("w", encoding="utf-8") as output:
        output.write("COMMAND: " + json.dumps(command) + "\n")
        output.flush()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        for line in process.stdout:
            output.write(line)
            output.flush()
            print(line, end="", flush=True)
        return process.wait()


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Integration tests execute exclusively on GitHub Actions")
    directory = Path(os.environ["EVIDENCE_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    collect = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    collection_exit = capture(collect, directory / "collected-tests.txt")
    (directory / "collection-exit-code.txt").write_text(str(collection_exit) + "\n")
    if collection_exit:
        return collection_exit
    command = [sys.executable, "-m", "pytest", "-ra", "-vv", "--color=no",
               f"--junitxml={directory / 'junit.xml'}"]
    exit_code = capture(command, directory / "test-output.txt")
    (directory / "exit-code.txt").write_text(str(exit_code) + "\n")
    (directory / "execution.json").write_text(json.dumps({
        "suite_command": command, "collection_command": collect,
        "collection_exit_code": collection_exit, "suite_exit_code": exit_code,
        "started_at_utc": started, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
