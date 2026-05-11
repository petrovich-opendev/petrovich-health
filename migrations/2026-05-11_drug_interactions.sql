-- Migration: drug_interactions_v1 (curated DDI knowledge base)
-- Date: 2026-05-11
-- Purpose: backbone for DDI checker called from /protocol and /ddi.
--
-- This is a SEED dataset (~25 pairs) curated around the PROMOMED portfolio:
-- GLP-1 (tirzepatide/semaglutide), apalutamide, lenalidomide, plus
-- generic high-prevalence pairs (statins, warfarin, levothyroxine).
-- For production scale, swap this load with DDInter 2.0 / OpenFDA labels —
-- the table shape stays the same, only the source pipeline changes.

CREATE TABLE IF NOT EXISTS drug_interactions_v1 (
    id UUID DEFAULT generateUUIDv4(),
    drug_a_inn String,           -- normalised INN, lowercase
    drug_b_inn String,           -- normalised INN, lowercase
    severity LowCardinality(String),  -- major | moderate | minor
    mechanism String,
    recommendation String,
    source String,
    loaded_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY (drug_a_inn, drug_b_inn);
