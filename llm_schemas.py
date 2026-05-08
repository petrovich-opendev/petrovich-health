"""JSON schemas for structured LLM outputs."""
from __future__ import annotations

CLASSIFY_DOCUMENT_SCHEMA = {
    "name": "classify_document",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["doc_class", "doc_type", "title", "collected_at", "lab_name", "summary"],
        "properties": {
            "doc_class": {"type": "string"},
            "doc_type": {"type": "string"},
            "title": {"type": "string"},
            "collected_at": {"type": ["string", "null"]},
            "lab_name": {"type": ["string", "null"]},
            "summary": {"type": "string"},
        },
    },
}


EXTRACT_BIOMARKERS_SCHEMA = {
    "name": "extract_biomarkers",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["collected_at", "lab_name", "results"],
        "properties": {
            "collected_at": {"type": ["string", "null"]},
            "lab_name": {"type": ["string", "null"]},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "biomarker",
                        "biomarker_original",
                        "category",
                        "value",
                        "unit",
                        "ref_low",
                        "ref_high",
                    ],
                    "properties": {
                        "biomarker": {"type": "string"},
                        "biomarker_original": {"type": "string"},
                        "category": {"type": "string"},
                        "value": {"type": ["number", "string"]},
                        "unit": {"type": "string"},
                        "ref_low": {"type": ["number", "null"]},
                        "ref_high": {"type": ["number", "null"]},
                    },
                },
            },
        },
    },
}


DIGEST_SCHEMA = {
    "name": "daily_digest",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["digest", "topics", "user_concerns", "new_info"],
        "properties": {
            "digest": {"type": "string"},
            "topics": {"type": "array", "items": {"type": "string"}},
            "user_concerns": {"type": "string"},
            "new_info": {"type": "string"},
        },
    },
}


PROFILE_SCHEMA = {
    "name": "health_profile",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "overall_status",
            "profile_text",
            "key_findings",
            "watchlist",
            "correlations",
            "missing_data",
            "alerts",
        ],
        "properties": {
            "overall_status": {"type": "string"},
            "profile_text": {"type": "string"},
            "key_findings": {"type": "array", "items": {"type": "string"}},
            "watchlist": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["biomarker", "trend", "last_value", "last_date", "action"],
                    "properties": {
                        "biomarker": {"type": "string"},
                        "trend": {"type": "string"},
                        "last_value": {"type": "string"},
                        "last_date": {"type": "string"},
                        "action": {"type": "string"},
                    },
                },
            },
            "correlations": {"type": "array", "items": {"type": "string"}},
            "missing_data": {"type": "array", "items": {"type": "string"}},
            "alerts": {"type": "array", "items": {"type": "string"}},
        },
    },
}
