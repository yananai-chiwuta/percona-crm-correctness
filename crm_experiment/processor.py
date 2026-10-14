"""Compare event-ID deduplication with entity-revision ordering in PostgreSQL."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


FIELDS = ("company", "job_title", "lead_status")
BIGINT_MAX = 2**63 - 1


@dataclass(frozen=True)
class Event:
    tenant_id: str
    provider: str
    event_id: str
    contact_id: str
    source_revision: int | None
    snapshot: dict[str, str | None]


@dataclass(frozen=True)
class ProcessingResult:
    outcome: str
    original_outcome: str | None
    reason: str
    applied: bool


Hook = Callable[[str, psycopg.Connection], None]
Trace = Callable[[dict[str, Any]], None]


def _validated_snapshot(event: Event) -> dict[str, str | None]:
    for field in ("tenant_id", "provider", "event_id", "contact_id"):
        value = getattr(event, field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a nonempty string")
    revision = event.source_revision
    if revision is not None and (
        type(revision) is not int or not 1 <= revision <= BIGINT_MAX
    ):
        raise ValueError("source_revision must be a positive bigint or None")
    if not isinstance(event.snapshot, dict) or set(event.snapshot) != set(FIELDS):
        raise ValueError("snapshot must contain exactly the three enrichment fields")
    normalized = {name: event.snapshot[name] for name in FIELDS}
    if any(value is not None and not isinstance(value, str) for value in normalized.values()):
        raise ValueError("snapshot values must be strings or null")
    return normalized


def process_event(
    conn: psycopg.Connection,
    event: Event,
    *,
    mode: str = "corrected",
    hook: Hook | None = None,
    trace: Trace | None = None,
) -> ProcessingResult:
    """Commit event and local contact together, or raise and roll back both.

    Requires an idle autocommit connection. Hooks expose test stages only;
    after_commit may simulate a lost acknowledgment after durable success.
    """
    snapshot = _validated_snapshot(event)  # No database writes for invalid envelopes.
    if mode not in ("naive", "corrected"):
        raise ValueError("mode must be naive or corrected")
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("process_event requires an idle autocommit connection")

    identity = (event.tenant_id, event.provider, event.event_id)
    contact_key = (event.tenant_id, event.provider, event.contact_id)
    committed = False

    def emit(kind: str, **details: Any) -> None:
        if trace:
            trace({
                "kind": kind,
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "backend_pid": conn.info.backend_pid,
                "mode": mode,
                "tenant_id": event.tenant_id,
                "provider": event.provider,
                "event_id": event.event_id,
                "contact_id": event.contact_id,
                "source_revision": event.source_revision,
                **details,
            })

    def stage(name: str) -> None:
        emit("transaction_phase", stage=name)
        if hook:
            hook(name, conn)

    emit("delivery_started", snapshot=snapshot)
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                cur.execute(
                    """INSERT INTO inbound_events
                       (tenant_id, provider, event_id, contact_id, source_revision, snapshot)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (tenant_id, provider, event_id) DO NOTHING
                       RETURNING event_id""",
                    (*identity, event.contact_id, event.source_revision, Jsonb(snapshot)),
                )
                claimed = cur.fetchone() is not None
                if not claimed:
                    # A new statement sees a concurrent winner after uniqueness wait.
                    cur.execute(
                        """SELECT contact_id, source_revision, snapshot, outcome
                           FROM inbound_events
                           WHERE tenant_id=%s AND provider=%s AND event_id=%s""",
                        identity,
                    )
                    original = cur.fetchone()
                    if original is None or original["outcome"] == "processing":
                        raise RuntimeError("event claim invariant violated")
                    same = (
                        original["contact_id"] == event.contact_id
                        and original["source_revision"] == event.source_revision
                        and original["snapshot"] == snapshot
                    )
                    result = ProcessingResult(
                        "duplicate" if same else "event_id_conflict",
                        original["outcome"],
                        "exact replay of committed event" if same else "event identity reused with changed content",
                        False,
                    )
                else:
                    stage("after_event_claim")
                    outcome = "unversioned"
                    reason = "no reliable provider revision; contact unchanged"
                    applied = False
                    if event.source_revision is not None:
                        predicate = (
                            "WHERE EXCLUDED.source_revision > crm_contacts.source_revision"
                            if mode == "corrected" else ""
                        )
                        cur.execute(
                            """INSERT INTO crm_contacts
                               (tenant_id, provider, contact_id, source_revision,
                                company, job_title, lead_status, last_applied_event)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                               ON CONFLICT (tenant_id, provider, contact_id) DO UPDATE SET
                                 source_revision=EXCLUDED.source_revision,
                                 company=EXCLUDED.company,
                                 job_title=EXCLUDED.job_title,
                                 lead_status=EXCLUDED.lead_status,
                                 last_applied_event=EXCLUDED.last_applied_event,
                                 application_count=crm_contacts.application_count + 1,
                                 updated_at=clock_timestamp()
                               """ + predicate + " RETURNING source_revision",
                            (*contact_key, event.source_revision,
                             *(snapshot[name] for name in FIELDS), event.event_id),
                        )
                        applied = cur.fetchone() is not None
                        # The conflict lock persists even when the predicate is false.
                        stage("after_contact_upsert")
                        if applied:
                            outcome = "applied"
                            reason = (
                                "unconditional control application" if mode == "naive"
                                else "contact created or strictly newer revision applied"
                            )
                        else:
                            cur.execute(
                                """SELECT source_revision, company, job_title, lead_status
                                   FROM crm_contacts
                                   WHERE tenant_id=%s AND provider=%s AND contact_id=%s""",
                                contact_key,
                            )
                            current = cur.fetchone()
                            if current is None:
                                raise RuntimeError("contact lock invariant violated")
                            # Fresh statement AFTER acquiring the contact lock; no CTE snapshot reuse.
                            cur.execute(
                                """SELECT EXISTS (
                                     SELECT 1 FROM inbound_events
                                     WHERE tenant_id=%s AND provider=%s AND contact_id=%s
                                       AND source_revision=%s AND event_id<>%s
                                       AND snapshot IS DISTINCT FROM %s::jsonb
                                   ) AS contradicted""",
                                (*contact_key, event.source_revision, event.event_id, Jsonb(snapshot)),
                            )
                            contradicted = cur.fetchone()["contradicted"]
                            equal_current_conflict = (
                                current["source_revision"] == event.source_revision
                                and any(current[name] != snapshot[name] for name in FIELDS)
                            )
                            if contradicted or equal_current_conflict:
                                outcome = "revision_conflict"
                                reason = "same source revision has differing retained snapshot evidence"
                            elif event.source_revision < current["source_revision"]:
                                outcome = "stale"
                                reason = "source revision is older than committed contact revision"
                            else:
                                outcome = "redundant"
                                reason = "equal current revision and identical snapshot; no contradiction"
                    cur.execute(
                        """UPDATE inbound_events SET outcome=%s, reason=%s,
                             processed_at=clock_timestamp()
                           WHERE tenant_id=%s AND provider=%s AND event_id=%s""",
                        (outcome, reason, *identity),
                    )
                    result = ProcessingResult(outcome, None, reason, applied)
                stage("before_commit")
        committed = True
        emit("delivery_committed", outcome=result.outcome,
             original_outcome=result.original_outcome, reason=result.reason, applied=result.applied)
        stage("after_commit")
        return result
    except BaseException as exc:
        emit("delivery_exception", exception_type=type(exc).__name__,
             exception_message=str(exc), committed=committed)
        raise
