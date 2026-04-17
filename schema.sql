-- Health Analytics Bot — ClickHouse schema
-- Run: clickhouse-client < schema.sql

CREATE DATABASE IF NOT EXISTS health_analytics;

-- Lab results (structured biomarkers)
CREATE TABLE IF NOT EXISTS health_analytics.lab_results (
    id UUID DEFAULT generateUUIDv4(),
    collected_at Date,
    uploaded_at DateTime DEFAULT now(),
    category LowCardinality(String),
    biomarker String,
    biomarker_original String,
    value Float64,
    unit String,
    ref_low Nullable(Float64),
    ref_high Nullable(Float64),
    is_abnormal Bool DEFAULT false,
    lab_name LowCardinality(String) DEFAULT '',
    source_file String,
    raw_text String DEFAULT '',
    notes String DEFAULT '',
    owner_id String
) ENGINE = MergeTree()
ORDER BY (owner_id, biomarker, collected_at);

-- Medical documents (EEG, Holter, MRI, consultations, etc.)
CREATE TABLE IF NOT EXISTS health_analytics.documents (
    id UUID DEFAULT generateUUIDv4(),
    uploaded_at DateTime DEFAULT now(),
    collected_at Date,
    doc_type LowCardinality(String),
    title String,
    lab_name LowCardinality(String) DEFAULT '',
    source_file String,
    full_text String,
    summary String DEFAULT '',
    owner_id String
) ENGINE = MergeTree()
ORDER BY (owner_id, doc_type, collected_at);

-- Upload audit log
CREATE TABLE IF NOT EXISTS health_analytics.upload_log (
    id UUID DEFAULT generateUUIDv4(),
    uploaded_at DateTime DEFAULT now(),
    source_file String,
    file_size_bytes UInt64 DEFAULT 0,
    pages UInt16 DEFAULT 0,
    biomarkers_extracted UInt16 DEFAULT 0,
    lab_name LowCardinality(String) DEFAULT '',
    collected_at Date,
    status LowCardinality(String) DEFAULT 'ok',
    error_message String DEFAULT '',
    raw_text String DEFAULT '',
    owner_id String
) ENGINE = MergeTree()
ORDER BY (owner_id, uploaded_at);

-- L0: Raw chat history
CREATE TABLE IF NOT EXISTS health_analytics.chat_log (
    ts DateTime DEFAULT now(),
    role LowCardinality(String),
    text String,
    message_id UInt64 DEFAULT 0,
    tokens_used UInt32 DEFAULT 0,
    owner_id String
) ENGINE = MergeTree()
ORDER BY (owner_id, ts);

-- L1: Daily chat digests
CREATE TABLE IF NOT EXISTS health_analytics.daily_digest (
    date Date,
    digest String,
    topics Array(String),
    user_concerns String DEFAULT '',
    new_info String DEFAULT '',
    model String DEFAULT '',
    owner_id String
) ENGINE = ReplacingMergeTree()
ORDER BY (owner_id, date);

-- L2: Cumulative health profile
CREATE TABLE IF NOT EXISTS health_analytics.health_profile (
    date Date,
    profile_text String,
    overall_status String DEFAULT '',
    key_findings String DEFAULT '',
    watchlist String DEFAULT '',
    correlations String DEFAULT '',
    missing_data String DEFAULT '',
    alerts String DEFAULT '',
    data_hash String DEFAULT '',
    model String DEFAULT '',
    owner_id String
) ENGINE = ReplacingMergeTree()
ORDER BY (owner_id, date);

-- Goals & nutrition targets
CREATE TABLE IF NOT EXISTS health_analytics.goals (
    id UUID DEFAULT generateUUIDv4(),
    owner_id String,
    created_at DateTime DEFAULT now(),
    active Bool DEFAULT true,
    goal_type LowCardinality(String),
    description String DEFAULT '',
    target_weight_kg Nullable(Float64),
    target_date Nullable(Date),
    current_weight_kg Float64,
    height_cm Float64,
    age UInt8,
    sex LowCardinality(String) DEFAULT 'male',
    activity_level LowCardinality(String),
    bmr Float64 DEFAULT 0,
    tdee Float64 DEFAULT 0,
    target_calories Float64 DEFAULT 0,
    protein_g Float64 DEFAULT 0,
    fat_g Float64 DEFAULT 0,
    carbs_g Float64 DEFAULT 0,
    leucine_target_g Float64 DEFAULT 0,
    medications String DEFAULT ''
) ENGINE = ReplacingMergeTree()
ORDER BY (owner_id, id);

-- Nutrition log (meals with amino acid profile)
CREATE TABLE IF NOT EXISTS health_analytics.nutrition_log (
    id UUID DEFAULT generateUUIDv4(),
    owner_id String,
    ts DateTime DEFAULT now(),
    meal_type LowCardinality(String) DEFAULT '',
    description String,
    calories Float64 DEFAULT 0,
    protein_g Float64 DEFAULT 0,
    fat_g Float64 DEFAULT 0,
    carbs_g Float64 DEFAULT 0,
    fiber_g Float64 DEFAULT 0,
    leucine_g Float64 DEFAULT 0,
    isoleucine_g Float64 DEFAULT 0,
    valine_g Float64 DEFAULT 0,
    lysine_g Float64 DEFAULT 0,
    methionine_g Float64 DEFAULT 0,
    threonine_g Float64 DEFAULT 0,
    tryptophan_g Float64 DEFAULT 0,
    phenylalanine_g Float64 DEFAULT 0,
    histidine_g Float64 DEFAULT 0,
    diaas_score Float64 DEFAULT 0,
    micronutrients String DEFAULT '',
    source LowCardinality(String) DEFAULT 'manual',
    raw_input String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY (owner_id, ts);

-- Body weight log
CREATE TABLE IF NOT EXISTS health_analytics.body_log (
    owner_id String,
    ts DateTime DEFAULT now(),
    weight_kg Float64,
    body_fat_pct Nullable(Float64),
    notes String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY (owner_id, ts);

-- Clinical lessons (self-learning)
CREATE TABLE IF NOT EXISTS health_analytics.clinical_lessons (
    id UUID DEFAULT generateUUIDv4(),
    created_at DateTime DEFAULT now(),
    condition String,
    lesson String,
    mechanism String DEFAULT '',
    evidence_level LowCardinality(String),
    source_refs String DEFAULT '',
    confirmed_count UInt32 DEFAULT 1,
    tags Array(String) DEFAULT []
) ENGINE = MergeTree()
ORDER BY (condition, created_at);

-- Reminders
CREATE TABLE IF NOT EXISTS health_analytics.reminders (
    id UUID DEFAULT generateUUIDv4(),
    owner_id String,
    chat_id String,
    text String,
    hour UInt8,
    minute UInt8,
    days Array(UInt8),
    active Bool DEFAULT true,
    created_at DateTime DEFAULT now(),
    last_sent_at Nullable(DateTime) DEFAULT NULL
) ENGINE = ReplacingMergeTree()
ORDER BY (owner_id, id);
