# Changelog

## [Unreleased] — 2026-04-20

### Claude-Desktop-style UX
- **Unified save → analysis**: sending lab results (PDF / photo / text) now produces one combined reply — saved biomarkers **plus** LLM analysis with trends, red flags, and next steps. No more two-step "ok, saved" / "now explain it" dance.
- Intermediate "Recognizing…" / "Parsing…" messages removed. Only `typing…` indicator remains.
- Photo OCR rewritten: `claude -p --add-dir` via the built-in Read tool, supports real iPhone-sized photos (the previous base64-in-argv path was broken at `ARG_MAX` for files >2 MB).

### Clinical reasoning upgrade
- `ask_llm` prompt rules expanded from 13 to 18. New: mandatory differential diagnosis (2–3 competing hypotheses), lab-artifact check before interpretation, serial-dynamics requirement, cross-system reasoning (liver × hormones, kidney × hematocrit, thyroid × lipids, Ca × albumin, electrolytes × anion gap), age-based cancer screening.
- `optimal_ranges.yaml` expanded 238 → 663 lines. Added: free T3/T4/rT3, PTH, ionized Ca, phosphorus, magnesium, albumin, Na/K/Cl/HCO3, MCV/MCH/RDW/reticulocytes, free testosterone, E2 sensitive (LC-MS/MS), DHEA-S, morning/evening cortisol, IGFBP-3, full lipid panel (LDL/HDL/TG/ApoB/Lp(a)), fibrinogen, D-dimer.
- New knowledge sections in `optimal_ranges.yaml`:
  - `heuristics` — De Ritis ratio, BUN/Cr, anion gap (with albumin correction), corrected Ca, MCV-based anemia classification, eGFR CKD-EPI, HOMA-IR, FIB-4, calculated osmolarity.
  - `lab_artifacts` — hemolysis, lipemia, tourniquet, muscle-mass creatinine inflation, creatine supplementation, wrong sampling time.
  - `cancer_screening` — general (PSA, colonoscopy, dermatoscopy, low-dose CT, echo + coronary Ca-score, DXA) + TRT-specific (testicular US, hematocrit + ferritin every 3 months).
- `nutrient_antagonists.yaml` expanded 171 → 300 lines. Added: grapefruit/CYP3A4, St John's Wort/induction, levothyroxine interactions (Ca/Fe/coffee/PPI wash-out 4 h), `hepatotoxic_drugs_warning` (paracetamol, amiodarone, methotrexate, isoniazid, nitrofurantoin, valproate, tamoxifen), nephrotoxic drugs, QT-prolonging classes (antiarrhythmics, macrolides, fluoroquinolones, antipsychotics, antiemetics), warfarin enhancers/reducers, serotonergic syndrome drug list.
- All three YAML knowledge bases (`optimal_ranges`, `nutrient_antagonists`, `protocols`) are now actually loaded into the LLM context. The previous version never read `optimal_ranges.yaml` from Python — 25 KB of clinical content was dormant.

### Data layer — breaking migrations
- `health_profile` migrated to `ReplacingMergeTree(updated_at) ORDER BY (owner_id, date)`. The old `ORDER BY (date)` allowed cross-user profile collisions on the same date after background merge.
- `lab_results` migrated to `ReplacingMergeTree(uploaded_at) ORDER BY (owner_id, biomarker, collected_at, value)`. Duplicate measurements (same biomarker + same day + same value uploaded twice, e.g. via PDF and then as text) now collapse automatically. `OPTIMIZE FINAL` run once cleared 18 existing duplicates.
- `FROM lab_results FINAL` in all LLM-context queries to see deduplicated data immediately.
- ClickHouse client now opens with `async_insert=0, wait_for_async_insert=1` so the "save → immediate LLM analysis" path never hits a visibility race.
- `_own()` helper now validates `owner_id` against `^-?\d{1,20}$` — SQL injection at f-string interpolation boundary is no longer possible even if callers pass untrusted input.

### Context builder fixes
- **Fresh uploads section first**: `query_for_llm_context` now prepends a `СВЕЖИЕ ЗАГРУЗКИ (48 часов)` block before `HEALTH PROFILE`. Rule 12 in the prompt tells the LLM to treat it as authoritative over the nightly L2 snapshot, so freshly uploaded labs are never shadowed by stale data.
- **Unit-aware dynamics**: trend rows grouped by `(biomarker, unit)` instead of just `biomarker`. Glucose in mmol/L and mg/dL no longer produce a nonsensical 1700%-jump trend line on unit changes.
- **Bounded series**: each biomarker shows the last 20 data points (`arraySlice`), not the full history.
- **Caps on fresh uploads**: `max_biomarkers=200, max_docs=20`.

### Reliability
- **`send_and_log` integrity**: Telegram delivery is tracked per-chunk. If all chunks fail (rate-limit / 403 / 400), `chat_log` is **not** written — the LLM no longer hallucinates "I already told you X" when the user never saw X. On partial delivery, a `[partial delivery]` marker is appended so the LLM knows the context is incomplete.
- **Reminder double-fire fixed**: `last_sent_at` was compared as MSK-aware with naive UTC, which made a reminder sent 3 h ago look "not yet sent this minute". Now everything compared in UTC; unit test confirms the 08:50 MSK → 08:50:58 MSK scenario now correctly suppresses duplicate.
- **Reminders logged to chat_log**: the background reminder thread now routes through `send_and_log`, so the LLM sees reminders as part of the conversation history.
- **`ask_llm` robust to CH outages**: `query_for_llm_context` and `query_recent_chat` are now wrapped in try/except; on failure the user gets "database temporarily unavailable" instead of a stack trace.
- **Generic error messages**: PDF / photo processing exceptions no longer leak paths or framework internals to the user. The full trace stays in logs.
- **Separate primary/fallback models**: `LLM_PRIMARY = claude-opus-4-7`, `LLM_FALLBACK = claude-sonnet-4-6`. Previously both were Opus, which made the fallback pointless at rate-limit time.

### Security
- **Prompt injection defence in `ask_llm`**: the user question is wrapped in `<user_input>…</user_input>` tags and the system prompt instructs the LLM to treat the content as data, not as instructions.
- **Prompt injection defence in `_finalize_save`**: tech report goes in `<save_report>…</save_report>`, user message in `<user_input>…</user_input>`; both payloads have their closing tags stripped before interpolation to defuse tag-escape attempts.

### Model unification
- Every Claude CLI call (`extractor`, `ask_llm`, workout parser, diagnostician) now uses `claude-opus-4-7`. The Haiku/Sonnet mix previously used for classification and extraction is gone.
