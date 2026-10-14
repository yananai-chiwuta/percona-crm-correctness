"""Each case owns committed setup, real independent sessions, and one schema."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from crm_experiment import process_event
from evidence import Evidence


CONTACT_SQL = """SELECT tenant_id, provider, contact_id, source_revision,
company, job_title, lead_status, last_applied_event, application_count, updated_at
FROM crm_contacts ORDER BY tenant_id, provider, contact_id"""
EVENT_SQL = """SELECT tenant_id, provider, event_id, contact_id, source_revision,
snapshot, outcome, received_at, processed_at, reason
FROM inbound_events ORDER BY tenant_id, provider, event_id"""
ACTIVITY_SQL = """SELECT pid, application_name, state, wait_event_type, wait_event,
query, pg_blocking_pids(pid) AS blocking_pids
FROM pg_stat_activity WHERE pid = ANY(%s) ORDER BY pid"""
LOCK_SQL = """SELECT pid, locktype, mode, granted, relation, page, tuple,
virtualxid, transactionid, classid, objid, objsubid
FROM pg_locks WHERE pid = ANY(%s)
ORDER BY pid, locktype, mode, granted, relation NULLS FIRST,
transactionid::text NULLS FIRST, virtualxid NULLS FIRST"""


class InjectedFailure(RuntimeError):
    pass


class Lab:
    def __init__(self, case, dsn, evidence):
        self.case, self.dsn, self.evidence = case, dsn, evidence
        self.schema = "case_" + uuid.uuid4().hex
        self.connections = []
        self.connection_lock = threading.Lock()
        self.observer = self.connect("observer", set_path=False)
        self.observer.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.set_path(self.observer)
        definition = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
        self.observer.execute(definition, prepare=False)
        self.main = self.connect("main")
        self.state("committed_setup")

    def set_path(self, conn):
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema)))

    def connect(self, label, set_path=True):
        conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row,
                               application_name=f"crm_{self.schema[-10:]}_{label}"[:63])
        with self.connection_lock:
            self.connections.append(conn)
        conn.execute("SET lock_timeout = '30s'")
        conn.execute("SET statement_timeout = '45s'")
        conn.execute("SET timezone = 'UTC'")
        if set_path:
            self.set_path(conn)
        return conn

    def query(self, statement, params=None, label="observer"):
        rows = self.observer.execute(statement, params).fetchall()
        self.evidence.query(self.case, label, statement, params, rows)
        return rows

    def state(self, label):
        return {"contacts": self.query(CONTACT_SQL, label=label + ":contacts"),
                "events": self.query(EVENT_SQL, label=label + ":events")}

    def invoke(self, conn, event, mode, hook=None):
        try:
            result = process_event(conn, event, mode=mode, hook=hook,
                                   trace=lambda entry: self.evidence.trace(self.case, entry))
        except BaseException as exc:
            self.evidence.append("deliveries.jsonl", {
                "case": self.case, "origin": "harness", "event": dataclasses.asdict(event),
                "backend_pid": conn.info.backend_pid, "exception_type": type(exc).__name__,
                "exception": str(exc), "transaction_status": conn.info.transaction_status.name})
            raise
        self.evidence.append("deliveries.jsonl", {
            "case": self.case, "origin": "harness", "event": dataclasses.asdict(event),
            "backend_pid": conn.info.backend_pid, "result": dataclasses.asdict(result),
            "transaction_status": conn.info.transaction_status.name})
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        return result

    def deliver(self, event, mode, hook=None):
        self.state("before:" + event.event_id)
        try:
            return self.invoke(self.main, event, mode, hook)
        finally:
            self.state("after:" + event.event_id)

    def locks(self, pids, label):
        activity = self.query(ACTIVITY_SQL, (pids,), label + ":activity")
        locks = self.query(LOCK_SQL, (pids,), label + ":locks")
        record = {"case": self.case, "label": label, "monotonic": time.monotonic(),
                  "pids": pids, "activity": activity, "locks": locks}
        self.evidence.append("locking.jsonl", record)
        return activity

    def wait_blocked(self, follower, leader):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            activity = self.locks([leader, follower], "await_block")
            if any(row["pid"] == follower and leader in row["blocking_pids"] for row in activity):
                return
            time.sleep(0.05)  # Observation interval, never a schedule gate.
        raise AssertionError(f"Required blocker {leader} for follower {follower} not observed")

    def pair(self, first, second, mode, *, stage="after_contact_upsert", rollback=False,
             blocked_check=None):
        """The first worker holds the specified lock until real blocking is proved."""
        acquired, release = threading.Event(), threading.Event()
        leader, follower = self.connect("leader"), self.connect("follower")
        pids = [leader.info.backend_pid, follower.info.backend_pid]
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="crm_case")
        futures = []

        def hook(actual, conn):
            self.evidence.phase(self.case, origin="harness", stage=actual,
                                backend_pid=conn.info.backend_pid,
                                visibility="committed" if actual == "after_commit" else "transaction_local")
            if actual == stage:
                acquired.set()
                if not release.wait(20):
                    raise AssertionError("Leader gate expired")
                if rollback:
                    raise InjectedFailure("rollback_after_lock")

        try:
            first_future = pool.submit(self.invoke, leader, first, mode, hook)
            futures.append(first_future)
            if not acquired.wait(20):
                if first_future.done():
                    first_future.result()
                raise AssertionError("Leader did not reach coordination gate")
            second_future = pool.submit(self.invoke, follower, second, mode)
            futures.append(second_future)
            self.wait_blocked(pids[1], pids[0])
            self.state("observer_while_workers_uncommitted")
            if blocked_check:
                blocked_check()
            release.set()
            if rollback:
                with pytest.raises(InjectedFailure, match="rollback_after_lock"):
                    first_future.result(timeout=60)
                first_result = None
            else:
                first_result = first_future.result(timeout=60)
            second_result = second_future.result(timeout=60)
            return first_result, second_result
        finally:
            release.set()
            for conn, future in zip((leader, follower), futures):
                if not future.done():
                    try:
                        conn.cancel()
                    except psycopg.Error as exc:
                        self.evidence.phase(self.case, stage="cancel_error", exception=str(exc))
            # Preserve the primary test exception, but prove teardown errors in evidence.
            for future in futures:
                try:
                    future.result(timeout=60)
                except BaseException as exc:
                    self.evidence.phase(self.case, stage="worker_teardown",
                                        exception_type=type(exc).__name__, exception=str(exc))
            pool.shutdown(wait=False, cancel_futures=True)
            self.locks(pids, "after_worker_teardown")
            self.state("after_worker_teardown")

    def failpoint(self, event, mode, stage, paused_check):
        reached, release = threading.Event(), threading.Event()
        worker = self.connect("failpoint")
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crm_failpoint")

        def hook(actual, conn):
            self.evidence.phase(self.case, origin="harness", stage=actual,
                                backend_pid=conn.info.backend_pid,
                                visibility="committed" if actual == "after_commit" else "transaction_local")
            if actual == stage:
                reached.set()
                if not release.wait(20):
                    raise AssertionError("Failpoint gate expired")
                raise InjectedFailure(stage)

        future = pool.submit(self.invoke, worker, event, mode, hook)
        try:
            if not reached.wait(20):
                if future.done():
                    future.result()
                raise AssertionError("Failpoint stage not reached")
            self.state("observer_at_failpoint:" + stage)
            paused_check()
            release.set()
            with pytest.raises(InjectedFailure, match=stage):
                future.result(timeout=60)
            assert worker.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        finally:
            release.set()
            if not future.done():
                worker.cancel()
            try:
                future.result(timeout=60)
            except BaseException as exc:
                self.evidence.phase(self.case, stage="failpoint_teardown",
                                    exception_type=type(exc).__name__, exception=str(exc))
            pool.shutdown(wait=False, cancel_futures=True)
            self.locks([worker.info.backend_pid], "after_failpoint_teardown")
            self.state("after_failpoint_teardown")

    def close(self):
        try:
            self.state("final_before_schema_drop")
        finally:
            for conn in self.connections:
                if conn is not self.observer and not conn.closed:
                    try:
                        conn.rollback()
                    finally:
                        conn.close()
            try:
                self.observer.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
            finally:
                self.observer.close()


@pytest.fixture(scope="session")
def evidence():
    directory = os.environ.get("EVIDENCE_DIR")
    if not directory:
        pytest.fail("EVIDENCE_DIR is required; no local evidence fallback")
    return Evidence(Path(directory))


@pytest.fixture
def lab(request, evidence):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.fail("DATABASE_URL is required; no local database fallback")
    instance = Lab(request.node.nodeid, dsn, evidence)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(params=["naive", "corrected"])
def mode(request):
    return request.param


def pytest_collection_finish(session):
    directory = os.environ.get("EVIDENCE_DIR")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        inventory = "collected-nodeids.json" if session.config.option.collectonly else "run-nodeids.json"
        (target / inventory).write_text(
            json.dumps([item.nodeid for item in session.items], indent=2) + "\n", encoding="utf-8")
