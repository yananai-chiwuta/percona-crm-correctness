-- Two-table fixture. The test harness selects a dedicated schema before executing this file.
CREATE TABLE inbound_events (
    tenant_id text NOT NULL CHECK (length(btrim(tenant_id)) > 0),
    provider text NOT NULL CHECK (length(btrim(provider)) > 0),
    event_id text NOT NULL CHECK (length(btrim(event_id)) > 0),
    contact_id text NOT NULL CHECK (length(btrim(contact_id)) > 0),
    source_revision bigint CHECK (source_revision > 0),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    outcome text NOT NULL DEFAULT 'processing'
        CHECK (outcome IN ('processing','applied','stale','redundant','revision_conflict','unversioned')),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    processed_at timestamptz,
    reason text NOT NULL DEFAULT 'transaction not finalized',
    PRIMARY KEY (tenant_id, provider, event_id)
);
CREATE INDEX inbound_revision_history
    ON inbound_events (tenant_id, provider, contact_id, source_revision);

CREATE TABLE crm_contacts (
    tenant_id text NOT NULL CHECK (length(btrim(tenant_id)) > 0),
    provider text NOT NULL CHECK (length(btrim(provider)) > 0),
    contact_id text NOT NULL CHECK (length(btrim(contact_id)) > 0),
    source_revision bigint NOT NULL CHECK (source_revision > 0),
    company text,
    job_title text,
    lead_status text,
    last_applied_event text NOT NULL,
    application_count bigint NOT NULL DEFAULT 1 CHECK (application_count > 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, provider, contact_id)
);
