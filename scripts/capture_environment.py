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

POSTGRES_17_IMAGE = (
    "postgres:17.11-bookworm@sha256:"
    "3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
)
PERCONA_18_IMAGE = (
    "percona/percona-distribution-postgresql:18.6.1-3@sha256:"
    "cf1ef553805898b73764a19276bf2fc9f78148a0215761ad7491c37c3ed31327"
)
TARGETS = {
    "postgresql-17.11": {
        "server_version_num": "170011",
        "image_reference": POSTGRES_17_IMAGE,
        "image_source_url": "https://hub.docker.com/_/postgres",
        "vendor": None,
    },
    "percona-postgresql-18": {
        "server_version_num": "180006",
        "image_reference": PERCONA_18_IMAGE,
        "image_source_url": "https://docs.percona.com/postgresql/18/docker/docker.html",
        "image_digest_source_url": "https://hub.docker.com/v2/repositories/percona/percona-distribution-postgresql/tags/18.6.1-3",
        "vendor": "Percona",
        "expected_repo_digests": [
            "sha256:cf1ef553805898b73764a19276bf2fc9f78148a0215761ad7491c37c3ed31327",
            "sha256:91ffd3f3cef11ebbac6e18211869a736dd1c8a7aa733c7a8c951c976c4afcfa4",
        ],
    },
}


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Experiment execution is restricted to GitHub Actions")
    output = Path(os.environ["EVIDENCE_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    target_name = os.environ["DATABASE_TARGET"]
    if target_name not in TARGETS:
        raise RuntimeError(f"Unknown database target: {target_name}")
    target = TARGETS[target_name]
    image_reference = os.environ["POSTGRES_IMAGE"]
    if image_reference != target["image_reference"]:
        raise RuntimeError("Workflow image reference differs from the reviewed pinned image")
    expected_version = os.environ["EXPECTED_POSTGRES_VERSION_NUM"]
    if expected_version != target["server_version_num"]:
        raise RuntimeError("Workflow PostgreSQL version expectation does not match the target")

    metadata = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": os.environ["GITHUB_REPOSITORY"],
        "source_sha": os.environ["GITHUB_SHA"],
        "checked_out_sha": command("git", "rev-parse", "HEAD"),
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "database_target": target_name,
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
        "service_image_reference": image_reference,
        "expected_server_version_num": expected_version,
        "image_source_url": target["image_source_url"],
        "image_digest_source_url": target.get("image_digest_source_url"),
        "expected_image_vendor": target["vendor"],
    }
    container_id = os.environ["POSTGRES_SERVICE_ID"]
    service = json.loads(command("docker", "inspect", container_id))[0]
    image = json.loads(command("docker", "image", "inspect", service["Image"]))[0]
    labels = image.get("Config", {}).get("Labels") or {}
    metadata["service_container_id"] = container_id
    metadata["service_config_image"] = service.get("Config", {}).get("Image")
    metadata["image_id"] = service["Image"]
    metadata["image_repo_digests"] = image.get("RepoDigests", [])
    metadata["image_architecture"] = image["Architecture"]
    metadata["image_os"] = image["Os"]
    metadata["image_labels"] = {
        key: labels.get(key) for key in (
            "name", "vendor", "org.opencontainers.image.version",
            "org.opencontainers.image.source", "org.opencontainers.image.revision")
    }
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
    assert metadata["image_os"] == "linux"
    assert metadata["image_architecture"] == "amd64", "unexpected service image architecture"
    assert metadata["service_config_image"] == image_reference, "service image reference mismatch"
    assert settings["server_version_num"] == expected_version, "unexpected PostgreSQL version for target"
    assert settings["encoding"] == "UTF8"
    assert settings["timezone"] == "UTC"
    assert settings["default_isolation"] == settings["session_isolation"] == "read committed"
    assert settings["fsync"] == settings["synchronous_commit"] == settings["track_activities"] == "on"
    assert settings["existing_test_schemas"] == 0, "database was not initially clean"
    if target["vendor"]:
        assert labels.get("vendor") == target["vendor"], "service image is not labelled as Percona"
        assert os.environ["EXPECTED_IMAGE_VENDOR"] == target["vendor"]
        matching_repo_digests = [
            digest for digest in target["expected_repo_digests"]
            if any(digest in actual for actual in metadata["image_repo_digests"])
        ]
        assert matching_repo_digests, "pulled image digest does not match the pinned Percona registry manifest"
        metadata["verified_image_repo_digest"] = matching_repo_digests[0]
        metadata["verified_image_vendor_label"] = labels.get("vendor")
        (output / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        (output / "environment.md").write_text(
            "# Actual GitHub-hosted execution environment\n\n```json\n" +
            json.dumps(metadata, indent=2) + "\n```\n", encoding="utf-8")
    else:
        assert not os.environ["EXPECTED_IMAGE_VENDOR"], "unexpected image vendor expectation"
    for name, expected in (("psycopg", "3.3.6"), ("psycopg-binary", "3.3.6"), ("pytest", "9.1.1")):
        assert metadata["packages"][name] == expected
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
