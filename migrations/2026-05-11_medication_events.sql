-- Migration: medication_events_v1 + extend reminders with drug fields
-- Date: 2026-05-11
-- Purpose: PDC (Proportion of Days Covered) adherence dashboard

-- Drug ingestion events — every taken/skipped/forgot moment becomes a row.
-- Append-only; ReplacingMergeTree on (owner_id, drug_name, ts) to absorb
-- accidental double-click on inline button.
CREATE TABLE IF NOT EXISTS medication_events_v1 (
    id UUID DEFAULT generateUUIDv4(),
    owner_id String,
    ts DateTime,
    drug_name String,
    drug_inn String DEFAULT '',
    dose String DEFAULT '',
    status LowCardinality(String),          -- taken | skipped | forgot | na_unavailable | na_cost | na_ae
    source LowCardinality(String),          -- reminder_button | manual | voice
    reminder_id Nullable(UUID),
    note String DEFAULT ''
) ENGINE = ReplacingMergeTree(ts)
ORDER BY (owner_id, drug_name, ts);

-- Extend reminders: drug_name + drug_inn + dose lets PDC tie a reminder
-- firing back to a specific drug, instead of guessing from free text.
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS drug_name String DEFAULT '';
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS drug_inn String DEFAULT '';
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS dose String DEFAULT '';
