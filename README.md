# PostgreSQL CRM correctness under duplicate and out-of-order events

A small synthetic experiment comparing two PostgreSQL webhook handlers. Both
deduplicate event IDs and commit event recording, the local contact update and the
processing outcome together. The control applies every newly identified versioned
snapshot; the corrected handler updates a contact only for a strictly newer source
revision.

This is a database correctness experiment. It uses no real contacts, external CRM
service or production credentials, and makes no production-use or performance claim.
The fixture calls the local Python `process_event` function against its test database;
it does not implement an HTTP webhook receiver or communicate with a real CRM.

## What the experiment demonstrates

The verified T02 runs delivered revisions 10, 12 and then 11, each with a different
event ID. This table stops immediately after the first delayed revision 11:

| Handler | Before delayed revision 11 | After delayed revision 11 | Recorded event outcome |
|---|---|---|---|
| Control | Revision 12; ExampleCo-12 / Director / customer; application count 2 | Revision 11; ExampleCo-11 / Manager / qualified; application count 3 | applied |
| Corrected | Revision 12; ExampleCo-12 / Director / customer; application count 2 | Same revision, fields, count, last event and timestamp | stale |

The control's corruption is deliberately asserted as the expected defective state;
it is not hidden with an expected-failure marker. T02 later sends another distinct
older event, so its control counter ends at 4 rather than the table's intermediate 3.
See [tests/test_crm.py](tests/test_crm.py).

T03 coordinates two independent workers and a separate observer. The observer must
prove the follower is blocked by the leader using `pg_blocking_pids`,
`pg_stat_activity` and `pg_locks`. In [run 38649001953](https://github.com/yananai-chiwuta/percona-crm-correctness/actions/runs/38649001953),
against PostgreSQL 17.11, case
`test_T03_coordinated_revision_race[corrected-higher_first-existing]` recorded:

| Run ID | Database target | Repetition | Leader PID | Follower PID | Follower wait / blocker | Transaction-ID lock evidence |
|---|---|---:|---:|---:|---|---|
| 38649001953 | PostgreSQL 17.11 | 1 | 131 | 132 | Lock / transactionid; `pg_blocking_pids(132) = [131]` | XID 824: leader ExclusiveLock granted; follower ShareLock ungranted |
| 38649001953 | PostgreSQL 17.11 | 2 | 131 | 132 | Lock / transactionid; `pg_blocking_pids(132) = [131]` | XID 824: leader ExclusiveLock granted; follower ShareLock ungranted |

After the leader committed revision 12, the follower's revision 11 returned `stale`;
the contact retained revision 12, count 2 and last event `e12`. This demonstrates an
observed lock wait despite no stale contact update. PIDs and transaction IDs describe
this execution and can change on reruns. Both worker orders and existing/absent
contacts are tested; no latency or throughput comparison is made.

Evidence: the verified run linked below, both artifacts' `sql-state.txt` (T02
`before:e11:contacts`, `after:e11:contacts/events`; T03
`revision_race_final:contacts/events`), `locking.jsonl` (T03 `await_block`) and
`deliveries.jsonl`. Test definitions are in [test_crm.py](tests/test_crm.py), and
observer queries/coordination are in [conftest.py](tests/conftest.py).

## Design and ordering contract

- [inbound_events](schema.sql) identifies events by `(tenant_id, provider, event_id)`.
  Exact replays preserve the original event; reused IDs with changed content report
  `event_id_conflict` without overwriting it.
- `crm_contacts` identifies contacts by `(tenant_id, provider, contact_id)`.
  [process_event](crm_experiment/processor.py) uses one explicit READ COMMITTED
  transaction for all local database processing.
- Ordering comes from a trustworthy positive bigint provider revision per contact,
  not arrival time, event ID or a generated timestamp. Gaps are allowed. Snapshots
  contain exactly `company`, `job_title` and `lead_status`; explicit null clears a field.
- The corrected contact UPSERT adds
  `WHERE EXCLUDED.source_revision > crm_contacts.source_revision`.
  It then examines current state and retained same-revision history in fresh SQL
  statements while holding the contact conflict lock.
- Missing revisions are recorded as `unversioned` with no contact write. Identical
  equal revisions are `redundant` unless retained history contradicts them. Conflicting
  equal revisions are flagged as `revision_conflict`, preserving accepted contact state.
  This policy does not choose which conflicting payload is true.

Provider revision reliability is an input assumption, not something this synthetic
fixture verifies. PostgreSQL 17 documents the database mechanisms:
[INSERT ... ON CONFLICT](https://www.postgresql.org/docs/17/sql-insert.html#SQL-ON-CONFLICT)
and [transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html).

## Reproduce with GitHub Actions

No local Docker Desktop, database or Python environment is required. Execution is
restricted to GitHub-hosted Ubuntu using an ephemeral PostgreSQL service container.

1. Fork this repository into your own GitHub account. In the fork's **Actions** tab,
   enable workflows if GitHub prompts you. Check your account's current allowance
   and spending controls before dispatch; private repositories can consume included
   minutes and artifact storage. No repository secrets are required.
2. In **your fork**, open **Actions → CRM correctness experiment → Run workflow**,
   select `main` and dispatch once. The workflow is manual only: pushes, pull requests
   and schedules do not execute it.
3. Require all four jobs to pass for the same source commit, run and attempt: two
   independent clean repetitions for each target, PostgreSQL **17.11** and Percona
   Distribution for PostgreSQL **18.6.1**. Each runs all **85 parameterized
   integration cases** on a fresh PostgreSQL service, with isolated per-test schemas
   and real independent sessions.
4. Sign in to GitHub and download the four target-labelled
   `crm-evidence-RUN_ID-attempt-ATTEMPT-TARGET-repetition-N` artifacts. Workflow
   artifacts expire after **seven days**; the [v1.1.0 release](https://github.com/yananai-chiwuta/percona-crm-correctness/releases/tag/v1.1.0)
   provides the evidence bundles and their checksum manifest.

With authenticated GitHub CLI, replace `YOUR_GITHUB_LOGIN` with your fork owner:

```text
gh workflow run crm-correctness.yml --repo YOUR_GITHUB_LOGIN/percona-crm-correctness --ref main
gh run list --repo YOUR_GITHUB_LOGIN/percona-crm-correctness --workflow crm-correctness.yml
gh run view RUN_ID --repo YOUR_GITHUB_LOGIN/percona-crm-correctness
gh run download RUN_ID --repo YOUR_GITHUB_LOGIN/percona-crm-correctness --dir evidence/downloads/RUN_ID
```

The [workflow](.github/workflows/crm-correctness.yml) pins Python **3.12.15**,
PostgreSQL **17.11-bookworm** at
`postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826`,
and Percona Distribution for PostgreSQL **18.6.1** at
`percona/percona-distribution-postgresql:18.6.1-3@sha256:cf1ef553805898b73764a19276bf2fc9f78148a0215761ad7491c37c3ed31327`.
[requirements.lock](requirements.lock)
pins psycopg/binary **3.3.6**, pytest **9.1.1** and transitive dependencies with hashes.
The runner label is `ubuntu-24.04`; its changing image version is recorded.
Workflow permissions are `contents: read`, checkout credentials are not persisted,
and the only database credentials are disposable synthetic service values.

The exact cloud commands are in the workflow and [scripts/run_suite.py](scripts/run_suite.py):

```bash
python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock
python -m pip check
python scripts/capture_environment.py
python scripts/run_suite.py
python scripts/finalize_evidence.py
```

The suite runner records its actual interpreter and pytest arguments, collection
status and test exit code; do not treat these as instructions for local execution.

## Evidence and test coverage

[Verified public run 38649001953](https://github.com/yananai-chiwuta/percona-crm-correctness/actions/runs/38649001953),
attempt 1, tested commit **de00349697255cd4fd7e5b9d578c6a8eaf065478**:
[permanent verified source](https://github.com/yananai-chiwuta/percona-crm-correctness/tree/de00349697255cd4fd7e5b9d578c6a8eaf065478).

| Independent clean PostgreSQL run | Passed | Failures / errors / skips | Pytest exit | Verified blocking cases | Evidence |
|---|---|---|---|---|---|
| Repetition 1 | 85 | 0 / 0 / 0 | 0 | 26 | [Release evidence](https://github.com/yananai-chiwuta/percona-crm-correctness/releases/download/v1.1.0/crm-evidence-38649001953-attempt-1-repetition-1.zip) |
| Repetition 2 | 85 | 0 / 0 / 0 | 0 | 26 | [Release evidence](https://github.com/yananai-chiwuta/percona-crm-correctness/releases/download/v1.1.0/crm-evidence-38649001953-attempt-1-repetition-2.zip) |

Each job had a distinct fresh PostgreSQL 17.11 service and zero initial test schemas;
both used Python 3.12.15. Counts are recorded in `summary.json`, `junit.xml` and raw
`test-output.txt`; environment/source identities are in `environment.json` and
`source-checksums.sha256`. The T02/T03 observations above remain readable after
artifacts expire. Later documentation commits are not the originally tested SHA.

[Verified two-target run 38931356745](https://github.com/yananai-chiwuta/percona-crm-correctness/actions/runs/38931356745),
attempt 1, tested commit **648beac9e5b3dae6e77b9a172c6604693dae5161**:

| Database target | Repetition | Passed | Failures / errors / skips | Verified blocking cases | Evidence |
|---|---:|---:|---:|---:|---|
| PostgreSQL 17.11 | 1 | 85 | 0 / 0 / 0 | 26 | [Release evidence](https://github.com/yananai-chiwuta/percona-crm-correctness/releases/download/v1.1.0/crm-evidence-38931356745-attempt-1-postgresql-17.11-repetition-1.zip) |
| PostgreSQL 17.11 | 2 | 85 | 0 / 0 / 0 | 26 | [Release evidence](https://github.com/yananai-chiwuta/percona-crm-correctness/releases/download/v1.1.0/crm-evidence-38931356745-attempt-1-postgresql-17.11-repetition-2.zip) |
| Percona Distribution for PostgreSQL 18.6.1 | 1 | 85 | 0 / 0 / 0 | 26 | [Release evidence](https://github.com/yananai-chiwuta/percona-crm-correctness/releases/download/v1.1.0/crm-evidence-38931356745-attempt-1-percona-postgresql-18-repetition-1.zip) |
| Percona Distribution for PostgreSQL 18.6.1 | 2 | 85 | 0 / 0 / 0 | 26 | [Release evidence](https://github.com/yananai-chiwuta/percona-crm-correctness/releases/download/v1.1.0/crm-evidence-38931356745-attempt-1-percona-postgresql-18-repetition-2.zip) |

The 17.11 results above are separate from the original 17.11 run on **de00349697255cd4fd7e5b9d578c6a8eaf065478**.

Each artifact connects observations to the **actual release commit SHA**, workflow
run, attempt and repetition. It contains raw test output, JUnit, collection/execution
inventories, environment and image identities, dependency logs, exact SQL state
queries/results, delivery and transaction traces, blocker observations, service logs,
source checksums and artifact-file checksums. No earlier execution is implied for
a different commit. The evidence is recorded state and selected observations, not
an exhaustive trace of every SQL statement.

| Tests | Coverage |
|---|---|
| T01–T02 | Exact duplicate deliveries, identity reuse and out-of-order distinct IDs |
| T03–T04 | Both revision acquisition orders; existing/absent contacts; same-ID commit/rollback contenders |
| T05–T06 | Exceptions before commit roll back event and contact; lost acknowledgment after commit leaves one application |
| T07–T08 | Tenant/provider key scoping; equal-revision races and retained historical contradictions |
| T09–T10 | Unversioned events, explicit null clearing and invalid complete-snapshot inputs |

Tests assert complete contact state, event cardinality/outcomes, counters and
timestamps. Required blocker observations cannot be replaced with sleeps or skipped
tests. The evidence validator rejects missing scenarios, skipped/failed/error cases,
nonzero exit codes and incomplete evidence. A green upload step alone is not a pass.
Service initialization failure can leave only GitHub setup logs; do not claim success.

## Assumptions and limitations

Results concern declared synthetic schedules with complete source-owned snapshots,
handler-only writes, retained event history and no deletion/recreation or revision
reset. They do not establish universal correctness, production readiness, crash or
power-loss recovery, performance, or a deadlock-free guarantee.

Partial updates, history pruning, cross-provider identity reconciliation, source-truth
resolution, queue infrastructure and real CRM integration are outside scope.
Tenant/provider namespace tests verify key scoping, not authorization or RLS.
PostgreSQL transactions cannot atomically guarantee effects on an external CRM API.

## Licence

This project is released under [MIT](LICENSE), covering this repository's original
code and documentation. Dependencies retain their respective licences. Article
publication terms are separate from this repository's code licence.
