"""Collect real cloud/runtime identities and prove an initially clean database."""

import importlib.metadata
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Experiment execution is restricted to GitHub Actions")
    output = Path(os.environ["EVIDENCE_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": os.environ["GITHUB_REPOSITORY"],
        "source_sha": os.environ["GITHUB_SHA"],
        "checked_out_sha": command("git", "rev-parse", "HEAD"),
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "repetition": os.environ["REPETITION"],
        "workflow": os.environ["GITHUB_WORKFLOW"],
        "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
        "workflow_sha": os.environ.get("GITHUB_WORKFLOW_SHA"),
        "run_url": f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
        "os": platform.platform(), "architecture": platform.machine(),
        "cpu_count": os.cpu_count(), "python": platform.python_version(),
        "runner_image_os": os.environ.get("ImageOS"),
        "runner_image_version": os.environ.get("ImageVersion"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "memory": command("free", "-b"),
        "docker_version": command("docker", "version", "--format", "{{.Server.Version}}"),
        "libpq_version": psycopg.pq.version(),
        "packages": {name: importlib.metadata.version(name) for name in (
            "psycopg", "psycopg-binary", "pytest", "iniconfig", "packaging",
            "pluggy", "pygments", "typing-extensions")},
        "service_image_reference": os.environ["POSTGRES_IMAGE"],
    }
    container_id = os.environ["POSTGRES_SERVICE_ID"]
    service = json.loads(command("docker", "inspect", container_id))[0]
    image = json.loads(command("docker", "image", "inspect", service["Image"]))[0]
    metadata["service_container_id"] = container_id
    metadata["image_id"] = service["Image"]
    metadata["image_repo_digests"] = image.get("RepoDigests", [])
    metadata["image_architecture"] = image["Architecture"]
    metadata["image_os"] = image["Os"]
    # Never record full inspect output: its environment includes disposable credentials.
    query = """SELECT version() AS version,
      current_setting('server_version_num') AS server_version_num,
      current_setting('server_encoding') AS encoding,
      current_setting('TimeZone') AS timezone,
      current_setting('default_transaction_isolation') AS default_isolation,
      current_setting('transaction_isolation') AS session_isolation,
      current_setting('fsync') AS fsync,
      current_setting('synchronous_commit') AS synchronous_commit,
      current_setting('track_activities') AS track_activities,
      current_database() AS database,
      (SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'case_%') AS existing_test_schemas"""
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET timezone='UTC'")
        settings = conn.execute(query).fetchone()
    metadata["postgresql"] = settings
    metadata["settings_query"] = query
    (output / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (output / "environment.md").write_text(
        "# Actual GitHub-hosted execution environment\n\n```json\n" +
        json.dumps(metadata, indent=2) + "\n```\n", encoding="utf-8")
    tracked = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout.decode().split("\0")
    (output / "source-checksums.sha256").write_text("".join(
        f"{hashlib.sha256(Path(name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(tracked) if name), encoding="utf-8")
    assert metadata["checked_out_sha"] == metadata["source_sha"], "source SHA mismatch"
    assert metadata["python"] == "3.12.15", "unexpected Python build"
    assert metadata["architecture"] == "x86_64", "unexpected runner architecture"
    assert settings["server_version_num"] == "170011", "unexpected PostgreSQL version"
    assert settings["encoding"] == "UTF8"
    assert settings["timezone"] == "UTC"
    assert settings["default_isolation"] == settings["session_isolation"] == "read committed"
    assert settings["fsync"] == settings["synchronous_commit"] == settings["track_activities"] == "on"
    assert settings["existing_test_schemas"] == 0, "database was not initially clean"
    for name, expected in (("psycopg", "3.3.6"), ("psycopg-binary", "3.3.6"), ("pytest", "9.1.1")):
        assert metadata["packages"][name] == expected
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
