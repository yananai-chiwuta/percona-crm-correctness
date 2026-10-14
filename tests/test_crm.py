"""T01–T10: committed-state oracles and observed PostgreSQL contention."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from crm_experiment import Event


def snapshot(revision):
    titles = {10: "Analyst", 11: "Manager", 12: "Director"}
    statuses = {10: "new", 11: "qualified", 12: "customer"}
    return {"company": f"ExampleCo-{revision}", "job_title": titles.get(revision, "Synthetic-title"),
            "lead_status": statuses.get(revision, "new")}


def event(revision=12, identity=None, *, tenant="tA", provider="pA", contact="c1", fields=None):
    return Event(tenant_id=tenant, provider=provider, event_id=identity or f"e{revision}",
                 contact_id=contact, source_revision=revision,
                 snapshot=dict(fields if fields is not None else snapshot(revision)))


def result_is(result, outcome):
    assert result.outcome == outcome
    assert result.applied is (outcome == "applied")
    assert isinstance(result.reason, str)


def contact_is(state, delivered, count):
    matches = [row for row in state["contacts"] if
               (row["tenant_id"], row["provider"], row["contact_id"]) ==
               (delivered.tenant_id, delivered.provider, delivered.contact_id)]
    assert len(matches) == 1
    row = matches[0]
    expected = {"tenant_id": delivered.tenant_id, "provider": delivered.provider,
                "contact_id": delivered.contact_id, "source_revision": delivered.source_revision,
                **delivered.snapshot, "last_applied_event": delivered.event_id,
                "application_count": count}
    assert {key: row[key] for key in expected} == expected
    assert isinstance(row["updated_at"], dt.datetime)
    assert row["updated_at"].tzinfo is not None
    return row


def event_is(state, delivered, outcome):
    matches = [row for row in state["events"] if
               (row["tenant_id"], row["provider"], row["event_id"]) ==
               (delivered.tenant_id, delivered.provider, delivered.event_id)]
    assert len(matches) == 1
    row = matches[0]
    expected = {"tenant_id": delivered.tenant_id, "provider": delivered.provider,
                "event_id": delivered.event_id, "contact_id": delivered.contact_id,
                "source_revision": delivered.source_revision, "snapshot": delivered.snapshot,
                "outcome": outcome}
    assert {key: row[key] for key in expected} == expected
    assert isinstance(row["reason"], str)
    assert isinstance(row["received_at"], dt.datetime)
    assert isinstance(row["processed_at"], dt.datetime)
    assert row["processed_at"] >= row["received_at"]
    return row


def seed(lab, mode, existing):
    if existing:
        original = event(10)
        result_is(lab.deliver(original, mode), "applied")
        state = lab.state("seed_assertion")
        contact_is(state, original, 1)
        event_is(state, original, "applied")
    return lab.state("baseline")


def unchanged_contacts(lab, baseline, label):
    actual = lab.state(label)
    assert actual["contacts"] == baseline["contacts"]
    return actual


def unchanged_state(lab, baseline, label):
    actual = lab.state(label)
    assert actual == baseline
    return actual


def test_T01_exact_duplicate_and_key_order(lab, mode):
    original = event(10)
    result_is(lab.deliver(original, mode), "applied")
    baseline = lab.state("applied_baseline")
    contact_is(baseline, original, 1)
    event_is(baseline, original, "applied")
    assert len(baseline["contacts"]) == len(baseline["events"]) == 1
    reordered = replace(original, snapshot=dict(reversed(list(original.snapshot.items()))))
    for replay in (original, original, reordered):
        result = lab.deliver(replay, mode)
        result_is(result, "duplicate")
        assert result.original_outcome == "applied"
        unchanged_state(lab, baseline, "duplicate_unchanged")


@pytest.mark.parametrize("change", ["contact", "revision", "snapshot", "unversioned_retrofit"])
def test_T01_reused_identity(lab, mode, change):
    original = event(10)
    if change == "unversioned_retrofit":
        original = replace(original, source_revision=None)
    result_is(lab.deliver(original, mode), "unversioned" if change == "unversioned_retrofit" else "applied")
    baseline = lab.state("original_identity")
    altered = {"contact": replace(original, contact_id="substituted-contact"),
               "revision": replace(original, source_revision=11),
               "snapshot": replace(original, snapshot={**original.snapshot, "job_title": "Conflicting-title"}),
               "unversioned_retrofit": replace(original, source_revision=10)}[change]
    result_is(lab.deliver(altered, mode), "event_id_conflict")
    unchanged_state(lab, baseline, "identity_conflict_unchanged")
    # Repetition must not modify the immutable original outcome or timestamps.
    result_is(lab.deliver(altered, mode), "event_id_conflict")
    unchanged_state(lab, baseline, "repeated_identity_conflict_unchanged")


def test_T02_sequential_stale_new_ids(lab, mode):
    e10, e12, e11 = event(10), event(12), event(11)
    for delivered, count in ((e10, 1), (e12, 2)):
        result_is(lab.deliver(delivered, mode), "applied")
        state = lab.state("sequential_applied")
        contact_is(state, delivered, count)
        event_is(state, delivered, "applied")
    baseline = lab.state("before_stale")
    expected = "applied" if mode == "naive" else "stale"
    for index, delivered in enumerate((e11, replace(e11, event_id="e11-identical"))):
        result_is(lab.deliver(delivered, mode), expected)
        state = lab.state("sequential_stale_assertion")
        event_is(state, delivered, expected)
        assert len(state["events"]) == 3 + index
        assert len(state["contacts"]) == 1
        if mode == "naive":
            contact_is(state, delivered, 3 + index)
        else:
            assert state["contacts"] == baseline["contacts"]
            contact_is(state, e12, 2)


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "existing"])
@pytest.mark.parametrize("first_revision", [12, 11], ids=["higher_first", "lower_first"])
def test_T03_coordinated_revision_race(lab, mode, existing, first_revision):
    baseline = seed(lab, mode, existing)
    first, second = event(first_revision), event(23 - first_revision)
    first_result, second_result = lab.pair(
        first, second, mode, blocked_check=lambda: unchanged_state(lab, baseline, "blocked_baseline"))
    result_is(first_result, "applied")
    ignored = mode == "corrected" and first_revision == 12
    result_is(second_result, "stale" if ignored else "applied")
    state = lab.state("revision_race_final")
    winner = first if ignored else second
    contact_is(state, winner, int(existing) + (1 if ignored else 2))
    event_is(state, first, "applied")
    event_is(state, second, "stale" if ignored else "applied")
    assert len(state["events"]) == int(existing) + 2
    assert len(state["contacts"]) == 1


@pytest.mark.parametrize("rollback", [False, True], ids=["leader_commit", "leader_rollback"])
@pytest.mark.parametrize("changed", [False, True], ids=["identical", "changed_content"])
def test_T04_concurrent_same_identity(lab, mode, rollback, changed):
    baseline = lab.state("empty_baseline")
    first = event(12)
    second = replace(event(13), event_id=first.event_id) if changed else first
    first_result, second_result = lab.pair(
        first, second, mode, stage="after_event_claim", rollback=rollback,
        blocked_check=lambda: unchanged_state(lab, baseline, "claim_not_visible"))
    if rollback:
        assert first_result is None
        result_is(second_result, "applied")
        winner = second
    else:
        result_is(first_result, "applied")
        result_is(second_result, "event_id_conflict" if changed else "duplicate")
        if not changed:
            assert second_result.original_outcome == "applied"
        winner = first
    state = lab.state("identity_race_final")
    contact_is(state, winner, 1)
    event_is(state, winner, "applied")
    assert len(state["contacts"]) == len(state["events"]) == 1


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "existing"])
@pytest.mark.parametrize("stage", ["after_event_claim", "after_contact_upsert"])
def test_T05_atomic_rollback_and_retry(lab, mode, existing, stage):
    baseline = seed(lab, mode, existing)
    delivered = event(12)
    lab.failpoint(delivered, mode, stage,
                  lambda: unchanged_state(lab, baseline, "uncommitted_failure_invisible"))
    unchanged_state(lab, baseline, "rollback_restored_baseline")
    result_is(lab.deliver(delivered, mode), "applied")
    state = lab.state("retry_committed")
    contact_is(state, delivered, int(existing) + 1)
    event_is(state, delivered, "applied")
    assert len(state["events"]) == int(existing) + 1


def test_T06_committed_lost_acknowledgement(lab, mode):
    delivered = event(12)

    def committed_check():
        state = lab.state("committed_before_lost_ack")
        contact_is(state, delivered, 1)
        event_is(state, delivered, "applied")
        assert len(state["contacts"]) == len(state["events"]) == 1

    lab.failpoint(delivered, mode, "after_commit", committed_check)
    baseline = lab.state("lost_ack_committed_baseline")
    committed_check()
    result = lab.deliver(delivered, mode)
    result_is(result, "duplicate")
    assert result.original_outcome == "applied"
    unchanged_state(lab, baseline, "lost_ack_retry_unchanged")


def test_T07_tenant_provider_namespace(lab, mode):
    expected = []
    for revision, tenant, provider in ((12, "tA", "pA"), (10, "tB", "pA"),
                                       (11, "tA", "pB"), (9, "tB", "pB")):
        delivered = event(revision, "same-event", tenant=tenant, provider=provider)
        before = lab.state("namespace_before")
        result_is(lab.deliver(delivered, mode), "applied")
        state = lab.state("namespace_after")
        expected.append(delivered)
        assert len(state["contacts"]) == len(state["events"]) == len(expected)
        for previous in expected:
            contact_is(state, previous, 1)
            event_is(state, previous, "applied")
        old_contacts = {tuple(row[key] for key in ("tenant_id", "provider", "contact_id")): row
                        for row in before["contacts"]}
        for row in state["contacts"]:
            key = tuple(row[key] for key in ("tenant_id", "provider", "contact_id"))
            if key in old_contacts:
                assert row == old_contacts[key]
        for row in before["events"]:
            assert row in state["events"]
    baseline = lab.state("all_namespaces")
    result = lab.deliver(expected[0], mode)
    result_is(result, "duplicate")
    unchanged_state(lab, baseline, "namespaced_duplicate")
    result_is(lab.deliver(replace(expected[0], contact_id="substituted"), mode), "event_id_conflict")
    unchanged_state(lab, baseline, "namespaced_identity_conflict")


def test_T08_current_equal_and_conflict_precedence(lab):
    original = event(12)
    result_is(lab.deliver(original, "corrected"), "applied")
    baseline = lab.state("equal_baseline")
    matching = replace(original, event_id="same-revision-identical")
    conflict = replace(original, event_id="same-revision-conflict",
                       snapshot={**original.snapshot, "job_title": "Conflicting-title"})
    for delivered, outcome in ((matching, "redundant"), (conflict, "revision_conflict"),
                               (replace(matching, event_id="match-after-contradiction"), "revision_conflict")):
        result_is(lab.deliver(delivered, "corrected"), outcome)
        state = unchanged_contacts(lab, baseline, "equal_no_overwrite")
        event_is(state, delivered, outcome)
        assert event_is(state, original, "applied") == baseline["events"][0]
    assert len(lab.state("equal_final")["events"]) == 4


@pytest.mark.parametrize("conflicting_first", [False, True], ids=["matching_first", "conflicting_first"])
def test_T08_seeded_mixed_equal_race(lab, conflicting_first):
    baseline = seed(lab, "corrected", True)
    matching = event(10, "matching-new-id")
    conflicting = event(10, "conflicting-new-id", fields={**snapshot(10), "job_title": "Conflicting-title"})
    first, second = (conflicting, matching) if conflicting_first else (matching, conflicting)
    results = lab.pair(first, second, "corrected",
                       blocked_check=lambda: unchanged_state(lab, baseline, "mixed_equal_blocked"))
    outcomes = ("revision_conflict", "revision_conflict") if conflicting_first else ("redundant", "revision_conflict")
    for result, delivered, outcome in zip(results, (first, second), outcomes):
        result_is(result, outcome)
        event_is(lab.state("mixed_equal_event"), delivered, outcome)
    state = unchanged_contacts(lab, baseline, "mixed_equal_seed_preserved")
    assert len(state["events"]) == 3
    assert event_is(state, event(10), "applied") == baseline["events"][0]


@pytest.mark.parametrize("first_worker", ["a", "b"])
@pytest.mark.parametrize("different", [False, True], ids=["identical", "conflicting"])
def test_T08_absent_equal_race(lab, first_worker, different):
    a = event(12, "equal-a")
    b = event(12, "equal-b", fields={**snapshot(12), "job_title": "Conflicting-title"} if different else snapshot(12))
    first, second = (a, b) if first_worker == "a" else (b, a)
    baseline = lab.state("absent_equal_baseline")
    first_result, second_result = lab.pair(
        first, second, "corrected", blocked_check=lambda: unchanged_state(lab, baseline, "equal_insert_blocked"))
    outcome = "revision_conflict" if different else "redundant"
    result_is(first_result, "applied")
    result_is(second_result, outcome)
    state = lab.state("equal_insert_final")
    contact_is(state, first, 1)
    event_is(state, first, "applied")
    event_is(state, second, outcome)
    assert len(state["contacts"]) == 1 and len(state["events"]) == 2


def test_T08_retained_historical_revision(lab):
    seed(lab, "corrected", True)
    current = event(12)
    result_is(lab.deliver(current, "corrected"), "applied")
    baseline = lab.state("historical_baseline")
    identical = event(10, "historical-identical")
    conflict = event(10, "historical-conflict", fields={**snapshot(10), "job_title": "Conflicting-title"})
    for delivered, outcome in ((identical, "stale"), (conflict, "revision_conflict"),
                               (replace(identical, event_id="historical-after-conflict"), "revision_conflict")):
        result_is(lab.deliver(delivered, "corrected"), outcome)
        state = unchanged_contacts(lab, baseline, "historical_unchanged")
        event_is(state, delivered, outcome)
        for row in baseline["events"]:
            assert row in state["events"]
    assert len(lab.state("historical_final")["events"]) == 5


@pytest.mark.parametrize("first_worker", ["a", "b"])
@pytest.mark.parametrize("different", [False, True], ids=["identical", "conflicting"])
def test_T08_previously_unseen_stale_history_race(lab, first_worker, different):
    current = event(12)
    result_is(lab.deliver(current, "corrected"), "applied")
    baseline = lab.state("new_stale_history_baseline")
    a = event(11, "stale-a")
    b = event(11, "stale-b", fields={**snapshot(11), "job_title": "Conflicting-title"} if different else snapshot(11))
    first, second = (a, b) if first_worker == "a" else (b, a)
    results = lab.pair(first, second, "corrected",
                       blocked_check=lambda: unchanged_state(lab, baseline, "stale_lock_blocked"))
    outcomes = ("stale", "revision_conflict" if different else "stale")
    for result, delivered, outcome in zip(results, (first, second), outcomes):
        result_is(result, outcome)
        event_is(lab.state("stale_history_event"), delivered, outcome)
    state = unchanged_contacts(lab, baseline, "stale_history_unchanged")
    assert len(state["events"]) == 3
    if different:
        repeated = replace(first, event_id="match-after-stale-contradiction")
        result_is(lab.deliver(repeated, "corrected"), "revision_conflict")
        event_is(unchanged_contacts(lab, baseline, "stale_contradiction_retained"), repeated, "revision_conflict")


def test_T08_naive_equal_conflicting_control(lab):
    first = event(12, "equal-first")
    second = event(12, "equal-second", fields={**snapshot(12), "job_title": "Conflicting-title"})
    for delivered, count in ((first, 1), (second, 2)):
        result_is(lab.deliver(delivered, "naive"), "applied")
        state = lab.state("naive_equal_applies")
        contact_is(state, delivered, count)
        event_is(state, delivered, "applied")
    assert len(state["events"]) == 2


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "existing"])
def test_T09_unversioned_record_and_duplicate(lab, mode, existing):
    baseline = seed(lab, mode, existing)
    delivered = replace(event(12, "unversioned"), source_revision=None)
    result_is(lab.deliver(delivered, mode), "unversioned")
    state = unchanged_contacts(lab, baseline, "unversioned_no_write")
    event_is(state, delivered, "unversioned")
    assert len(state["events"]) == int(existing) + 1
    result = lab.deliver(delivered, mode)
    result_is(result, "duplicate")
    assert result.original_outcome == "unversioned"
    unchanged_state(lab, state, "unversioned_duplicate_unchanged")


def test_T10_null_clearing_then_stale(lab, mode):
    seed(lab, mode, True)
    cleared = event(12, fields={**snapshot(12), "job_title": None})
    result_is(lab.deliver(cleared, mode), "applied")
    baseline = lab.state("null_cleared")
    contact_is(baseline, cleared, 2)
    stale = event(11)
    result_is(lab.deliver(stale, mode), "applied" if mode == "naive" else "stale")
    state = lab.state("after_stale_null")
    event_is(state, stale, "applied" if mode == "naive" else "stale")
    if mode == "naive":
        contact_is(state, stale, 3)
    else:
        assert state["contacts"] == baseline["contacts"]
        matching = replace(cleared, event_id="null-identical")
        empty = replace(cleared, event_id="null-versus-empty", snapshot={**cleared.snapshot, "job_title": ""})
        for delivered, outcome in ((matching, "redundant"), (empty, "revision_conflict")):
            result_is(lab.deliver(delivered, mode), outcome)
            event_is(unchanged_contacts(lab, baseline, "null_equal_unchanged"), delivered, outcome)


INVALID = [
    ("missing_snapshot_key", {"snapshot": {"company": "Example", "job_title": "Analyst"}}),
    ("extra_snapshot_key", {"snapshot": {**snapshot(12), "extra": "invalid"}}),
    ("numeric_field", {"snapshot": {**snapshot(12), "job_title": 42}}),
    ("boolean_field", {"snapshot": {**snapshot(12), "company": False}}),
    ("empty_tenant", {"tenant_id": ""}), ("empty_provider", {"provider": ""}),
    ("empty_event", {"event_id": ""}), ("empty_contact", {"contact_id": ""}),
    ("revision_bool", {"source_revision": True}), ("revision_string", {"source_revision": "12"}),
    ("revision_zero", {"source_revision": 0}), ("revision_negative", {"source_revision": -1}),
    ("revision_overflow", {"source_revision": 2**63}),
]


@pytest.mark.parametrize("name,changes", INVALID, ids=[item[0] for item in INVALID])
def test_T10_invalid_envelope_no_database_changes(lab, mode, name, changes):
    baseline = seed(lab, mode, True)
    delivered = replace(event(12, "invalid-delivery"), **changes)
    xid_sql = "SELECT pg_current_xact_id_if_assigned()::text AS xid"
    # Supporting session/state evidence only: an unassigned XID before and after
    # does not by itself prove that no intervening transaction was opened.
    # Validation-before-transaction is additionally inspected in the handler code.
    before_xid = lab.main.execute(xid_sql).fetchall()
    lab.evidence.query(lab.case, "invalid_before_xid", xid_sql, None, before_xid)
    assert before_xid == [{"xid": None}]
    seen_hooks = []
    with pytest.raises((ValueError, TypeError)):
        lab.deliver(delivered, mode, hook=lambda stage, conn: seen_hooks.append(stage))
    assert seen_hooks == []
    after_xid = lab.main.execute(xid_sql).fetchall()
    lab.evidence.query(lab.case, "invalid_after_xid", xid_sql, None, after_xid)
    assert after_xid == [{"xid": None}]
    unchanged_state(lab, baseline, "invalid_envelope_unchanged")
