"""Summarize actual JUnit/state evidence, validate completeness, and hash files."""

import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main():
    directory = Path(os.environ["EVIDENCE_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    problems = []
    required = ("environment.json", "environment.md", "test-output.txt", "exit-code.txt",
                "collected-tests.txt", "collected-nodeids.json", "run-nodeids.json", "junit.xml", "deliveries.jsonl",
                "transaction-phases.jsonl", "sql-state.txt", "locking.jsonl", "execution.json",
                "collection-exit-code.txt", "dependency-install.txt", "dependency-check.txt",
                "postgresql-service-output.txt", "source-checksums.sha256")
    for name in required:
        if not (directory / name).is_file() or (directory / name).stat().st_size == 0:
            problems.append(f"Missing/empty evidence file: {name}")
    cases = []
    if (directory / "junit.xml").is_file():
        try:
            cases = list(ET.parse(directory / "junit.xml").iter("testcase"))
        except ET.ParseError as exc:
            problems.append(f"Invalid JUnit: {exc}")
    statuses = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for case in cases:
        status = "failed" if case.find("failure") is not None else (
            "error" if case.find("error") is not None else (
                "skipped" if case.find("skipped") is not None else "passed"))
        statuses[status] += 1
    groups = {f"T{number:02d}" for number in range(1, 11)}
    found_groups = {match.group(0) for case in cases
                    for match in re.finditer(r"T\d{2}", case.get("name", ""))}
    if found_groups != groups:
        problems.append(f"Required scenario groups missing: {sorted(groups - found_groups)}")
    if len(cases) != 85:
        problems.append(f"Expected all 85 approved parameterized scenarios; found {len(cases)}")
    if (directory / "collected-nodeids.json").is_file():
        collected = json.loads((directory / "collected-nodeids.json").read_text(encoding="utf-8"))
        # Collection is the exact pre-run inventory, including all parameter schedules.
        names = [nodeid.split("::")[-1] for nodeid in collected]
        executed = [case.get("name") for case in cases]
        if sorted(names) != sorted(executed):
            problems.append("Collected test inventory differs from JUnit executed cases")
        if (directory / "run-nodeids.json").is_file():
            actual_collection = json.loads((directory / "run-nodeids.json").read_text(encoding="utf-8"))
            if actual_collection != collected:
                problems.append("Execution collection differs from preserved pre-run collection")
    if (directory / "collection-exit-code.txt").is_file():
        if (directory / "collection-exit-code.txt").read_text().strip() != "0":
            problems.append("Pre-run collection did not exit successfully")
    exit_code = None
    if (directory / "exit-code.txt").is_file():
        try:
            exit_code = int((directory / "exit-code.txt").read_text().strip())
        except ValueError:
            problems.append("Unparseable actual pytest exit code")
    if exit_code != 0:
        problems.append(f"Actual pytest exit code: {exit_code}")
    if statuses["failed"] or statuses["error"] or statuses["skipped"] or not cases:
        problems.append(f"Suite has failed/error/skipped cases or no cases: {statuses}")

    locks = read_jsonl(directory / "locking.jsonl") if (directory / "locking.jsonl").is_file() else []
    blocked_cases = {entry["case"] for entry in locks if any(
        row["pid"] == entry["pids"][-1] and entry["pids"][0] in row["blocking_pids"]
        for row in entry["activity"]) and len(entry["pids"]) == 2}
    expected_blocked = {nodeid for nodeid in (collected if "collected" in locals() else [])
                        if any(name in nodeid for name in (
                            "T03_", "T04_", "T08_seeded_mixed_equal_race",
                            "T08_absent_equal_race", "T08_previously_unseen_stale_history_race"))}
    if not expected_blocked.issubset(blocked_cases):
        problems.append(f"Missing proven blocking cases: {sorted(expected_blocked - blocked_cases)}")

    state_entries = read_jsonl(directory / "sql-state.txt") if (directory / "sql-state.txt").is_file() else []
    final_states = {entry["case"]: entry["rows"] for entry in state_entries
                    if entry["label"] == "final_before_schema_drop:contacts"}
    final_events = {entry["case"]: entry["rows"] for entry in state_entries
                    if entry["label"] == "final_before_schema_drop:events"}
    if "collected" in locals() and set(collected) != set(final_states):
        problems.append("Final committed CRM snapshots missing for collected cases")
    if "collected" in locals() and set(collected) != set(final_events):
        problems.append("Final committed inbound-event snapshots missing for collected cases")
    if any(row["outcome"] == "processing" or row["processed_at"] is None
           for rows in final_events.values() for row in rows):
        problems.append("Unfinalized committed inbound event observed")
    summary = {
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "repetition": os.environ["REPETITION"],
        "pytest_exit_code": exit_code, "test_count": len(cases), "statuses": statuses,
        "observed_blocking_case_count": len(blocked_cases), "problems": problems,
        "verification": "PASS" if not problems else "FAILED_OR_INCOMPLETE",
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    lines = ["# Recorded integration observations", "", "```json", json.dumps(summary, indent=2), "```", "",
             "## Sequential stale-event control", "",
             "| Handler | Before delayed revision 11 | After first delayed revision 11 | Event outcome |",
             "|---|---|---|---|"]
    for mode in ("naive", "corrected"):
        relevant = [entry for entry in state_entries
                    if "T02_sequential_stale_new_ids" in entry["case"] and f"[{mode}]" in entry["case"]]
        before = next((entry["rows"] for entry in relevant if entry["label"] == "before_stale:contacts"), [])
        after = next((entry["rows"] for entry in relevant if entry["label"] == "after:e11:contacts"), [])
        events = next((entry["rows"] for entry in relevant if entry["label"] == "after:e11:events"), [])
        outcome = next((row["outcome"] for row in events if row["event_id"] == "e11"), "not observed")
        brief = lambda rows: json.dumps([{key: row[key] for key in (
            "source_revision", "company", "job_title", "lead_status", "last_applied_event", "application_count")}
            for row in rows], sort_keys=True)
        lines.append(f"| {mode} | `{brief(before)}` | `{brief(after)}` | {outcome} |")
    lines += ["",
             "## Committed CRM state at case completion", "",
             "| Case | Observed final CRM tuple(s) |", "|---|---|"]
    for nodeid, rows in sorted(final_states.items()):
        lines.append(f"| {nodeid.replace('|', '&#124;')} | `{json.dumps(rows, sort_keys=True).replace('|', '&#124;')}` |")
    lines += ["", "Exact before/after queries and outputs are in sql-state.txt. Worker observations",
              "are in locking.jsonl and transaction-phases.jsonl; delivery outcomes are in deliveries.jsonl.",
              "Control corruption is an expected asserted observation. No performance benchmark was run.",
              "The independent reviewer must still examine this evidence before conclusions are accepted."]
    (directory / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    total = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
    if total > 5 * 1024 * 1024:
        problems.append(f"Artifact exceeds 5 MiB upload cap: {total} bytes")
        summary["verification"] = "FAILED_OR_INCOMPLETE"
        (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        lines[3] = json.dumps(summary, indent=2)
        (directory / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Do not upload above the cap, and never truncate evidence to conceal it.
        print("\n".join(problems), file=sys.stderr)
        return 2
    (directory / "checksums.sha256").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(directory).as_posix()}\n"
        for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "checksums.sha256"
    ), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
