# PostgreSQL CRM correctness under duplicate and out-of-order events

A small synthetic experiment comparing two PostgreSQL webhook handlers. Both
deduplicate event IDs and commit event recording, the local contact update and the
processing outcome together. The control applies every newly identified versioned
snapshot; the corrected handler updates a contact only for a strictly newer source
revision.

This is a database correctness experiment. It uses no real contacts, external CRM
service or production credentials, and makes no production-use or performance claim.

## What the experiment demonstrates

T02 delivers revisions 10, 12 and then 11, each with a different event ID:

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
`pg_stat_activity` and `pg_locks`. A stale conditional UPSERT can wait before it
returns without changing the contact. Both worker orders and existing/absent contacts
are tested; no latency or throughput comparison is made.

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

## Reproduce with GitHub Actions

No local Docker Desktop, database or Python environment is required. Execution is
restricted to GitHub-hosted Ubuntu using an ephemeral PostgreSQL service container.

1. Use this repository with write access, or create your own copy and enable Actions.
   Check your current Actions allowance and spending controls before dispatch;
   private copies can consume included minutes and artifact storage.
2. Open **Actions → CRM correctness experiment → Run workflow**, select `main` and
   dispatch once. The workflow is manual only: pushes, pull requests and schedules
   do not execute it.
3. Require both repetition jobs to pass for the same source commit, run and attempt.
   Each runs all **85 parameterized integration cases** on a fresh independent
   PostgreSQL service, with isolated per-test schemas and real independent sessions.
4. Download both `crm-evidence-RUN_ID-attempt-ATTEMPT-repetition-N` artifacts. They
   expire after **seven days**. Preserve them if longer retention is needed.

For this repository, the equivalent dispatch and inspection commands are:

```text
gh workflow run crm-correctness.yml --repo yananai-chiwuta/percona-crm-correctness --ref main
gh run list --repo yananai-chiwuta/percona-crm-correctness --workflow crm-correctness.yml
gh run view RUN_ID --repo yananai-chiwuta/percona-crm-correctness
gh run download RUN_ID --repo yananai-chiwuta/percona-crm-correctness --dir evidence/downloads/RUN_ID
```

The [workflow](.github/workflows/crm-correctness.yml) pins Python **3.12.15** and
PostgreSQL **17.11-bookworm** by image digest. [requirements.lock](requirements.lock)
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

Inspect this repository's [manual workflow runs](https://github.com/yananai-chiwuta/percona-crm-correctness/actions/workflows/crm-correctness.yml).
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

The proposed release licence is [MIT](LICENSE), covering this repository's original
code and documentation. Dependencies retain their respective licences. Article
publication terms are separate from this repository's code licence.
