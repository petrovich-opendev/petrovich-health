# Petrovich Health Bot

Personal health & fitness AI assistant for Telegram. Tracks lab results, analyzes workout progression, profiles nutrition down to amino acids, and learns your terminology over time.

**Bot:** [@Petrovich_bio_bot](https://t.me/Petrovich_bio_bot)

## What It Does

### Lab Results & Medical Documents
- **PDF / Photo / Text** — send lab results in any format, the bot extracts biomarkers automatically
- **Confirmation gate** — before any extracted row lands in the database the bot shows an annotated preview (every biomarker with reference range, `[ABNORMAL]` flag where relevant) and three inline buttons: ✅ Accept / ✏️ Edit / ❌ Cancel. Nothing is silently inserted on a hallucinated unit or wrong value.
- **Unified save → analysis** — one combined reply: saved biomarkers + LLM analysis with trends, red flags, and next steps. No "ok, saved" / "now explain" two-step dance.
- **Self-check verification layer** — every extraction is checked by a second LLM pass against the raw text (kill-switch `LLM_SELFCHECK=0`)
- **Trend tracking** — monitor any biomarker over time with SPC control charts; unit-aware series (mmol/L vs mg/dL separated automatically)
- **Abnormal detection** — flags values outside reference ranges
- **Cross-system correlations** — links related biomarkers (liver panel, lipids, hormones, etc.)
- **Medical documents** — stores consultations, EEG, MRI, ultrasound reports for full-text search
- **Deduplication** — every CH insert is guarded by `dedup.py` (pre-insert duplicate-key check) on top of `ReplacingMergeTree`; same report uploaded twice (PDF + text paste) collapses into one series

### Protocol Tracking & Drug Interactions
- **`/protocol`** — current stack (TRT/GH/peptides/AI/SERM) inferred from the rolling 90-day digest window, with follow-up lab due-dates (ALT @ +6w on AAS, IGF-1 @ +8w on GH, E2 @ +4w on estrogenic compounds)
- **Auto-DDI scan** — `/protocol` quietly cross-checks the active stack against the drug-interaction table and surfaces `🚨 major / ⚠️ moderate` hits in-place; no extra command to remember
- **`/ddi <drug>`** — direct interaction lookup for a single drug, normalises trade names to INN (Тирзетта → тирзепатид, Velgia → semaglutide, Эрлеада → апалутамид, etc.). Curated 25-pair seed with FDA-label and clinical-rec citations; production-ready to swap for DDInter 2.0 / DrugBank
- **`/adherence`** — Proportion of Days Covered (PDC) per drug over a 30-day window with `days_supply` weighting (weekly injectables count as 7 days of coverage per take), threshold 0.80, miss-reason breakdown (`forgot / na_ae / na_unavailable / na_cost`). Driven by inline ✅/⏭️/🤔 buttons attached to drug reminders.

### Proactive Alerts
- **`/alerts`** — three checks combined: lab-fatigue (test overdue by >X months), SPC trend reversals (regression after a streak of improvement), and `/adherence`-below-threshold drugs
- **Non-pushy** — no daily auto-reports; everything is on-demand. Error notifications to admin only.

### Workout Tracking & Analytics
- **Paste from Apple Notes** — copy your training log, the bot recognizes it automatically (no commands needed)
- **Adaptive glossary** — understands 70+ Russian bodybuilding abbreviations (RPE, grips, rep modifiers, exercise names)
- **Per-exercise analytics:**
  - Weight progression across weeks
  - Volume tracking (sets x reps x weight)
  - RPE/effort trends
  - Bodyweight exercise rep progression
- **Verdicts** — labels each exercise: progression / plateau / regression / grinding
- **Recommendations** — suggests deload, variation changes, or to keep pushing

### Nutrition & Amino Acid Profiling
- **Full amino acid tracking** — all 9 essential amino acids, not just protein/carbs/fats
- **DIAAS scoring** — protein quality assessment per meal
- **Leucine threshold** — tracks mTOR activation threshold (2.5g per meal)
- **Daily totals** — automatic calorie and macro summation
- **Goal-based targets** — calculates BMR/TDEE/macros based on your parameters

### Natural Language Reminders
- **Just type naturally** — "remind me tomorrow at 9:00 to check test results"
- **One-shot & recurring** — single date reminders auto-deactivate after sending
- **Understands Russian** — "napomni zavtra v 9:00", "cherez 2 chasa"

### Global Adaptive Glossary
- **Self-learning** — the bot builds a knowledge base from every interaction
- **No personal data** — only terms and definitions, shared across all users
- **Auto-verified** — lab biomarkers and parsed exercises get verified status
- **Unknown tracking** — new abbreviations become candidates for review
- **74+ workout terms** seeded, growing with every training log

### User Self-Registration
- **No manual setup** — new user writes to the bot, admin gets a notification
- **Works without @username** — phone-only Telegram accounts supported
- **Admin approves** with `/approve <chat_id>` — user gets instant access
- **Data isolation** — each user sees only their own data, shared glossary

### Clinical Decision Support
The LLM is primed with clinical reasoning rules and three knowledge bases:

- **Differential diagnosis** — 2–3 competing hypotheses with pro/con, not the first plausible one
- **Lab-artifact awareness** — checks for hemolysis, lipemia, prolonged tourniquet, muscle-inflated creatinine, post-workout AST/CK, wrong sampling time (cortisol AM/PM, testosterone trough) before making a call
- **Serial-dynamics mandatory** — single out-of-range value → "repeat in N days" instead of diagnosis
- **Cross-system reasoning** — liver × hormones, kidney × hematocrit × TRT, thyroid × lipids, Ca × albumin, electrolytes → anion gap, MCV × RDW anemia classification
- **Diagnostic heuristics built-in** — De Ritis ratio, BUN/Cr, anion gap (with albumin correction), corrected Ca, MCV-based anemia classification, eGFR CKD-EPI, HOMA-IR, FIB-4, calculated osmolarity
- **Age-based cancer screening** — PSA, colonoscopy, dermatoscopy, low-dose CT, echo + coronary Ca-score, DXA, plus TRT-specific (testicular US, HCT + ferritin every 3 months)

### Knowledge Bases (in-repo YAML)

- **`optimal_ranges.yaml`** — ~40 biomarkers with lab reference + evidence-based optimal ranges, critical thresholds, notes, and relevant interactions. Includes `heuristics`, `lab_artifacts`, `cancer_screening` sections.
- **`nutrient_antagonists.yaml`** — mineral antagonisms (Ca×Fe, Zn×Cu, Ca×Mg), drug-nutrient depletions (metformin → B12, PPI → Mg/B12, statins → CoQ10), drug-GI effects (GLP-1, NSAIDs, 17α-AAS hepatotoxicity), CYP3A4 interactions (grapefruit), levothyroxine wash-out rules, hepatotoxic / nephrotoxic / QT-prolonging drug classes, warfarin interaction matrix, serotonergic syndrome drug list.
- **`protocols.yaml`** — 32 compounds: dosing, half-lives, monitoring schedules, stack guidance for TRT / GH / peptides / AI / SERM / hCG.

### LLM-Powered Q&A
- Ask any health question — the bot answers with your actual lab data, not generic advice
- Full conversation context — every bot message goes back into `chat_log` so the LLM sees it next turn (no hallucinated "I told you X")
- Protocol knowledge base — dosing, half-lives, interactions for TRT / peptides / supplements
- Direct answers — no "consult your doctor" copouts
- **Prompt-injection resistant** — user input is wrapped in `<user_input>` tags; the system prompt instructs the LLM to treat tag contents as data, not instructions

## Architecture

```
Telegram Message
  |
  v
Auth (users.yaml) ── unknown? → access request → admin /approve
  |
  v
Universal Classifier (Python, no LLM)
  |
  +---> Medical data --> LLM extraction --> ClickHouse
  +---> Workout log  --> LLM parser + glossary --> Analytics --> ClickHouse
  +---> Reminder     --> Natural language parser --> Scheduler
  +---> Question     --> LLM Q&A with full health context
  |
  v
Global Glossary (auto-learning from every interaction)
```

- **Codebase split** — the bot is a thin entry-point glue (`tg_listener.py`) plus eight focused modules: `tg_users` (auth), `tg_transport` (TG primitives), `tg_context` (LLM context builders), `tg_llm` (`ask_llm`), `tg_documents` (PDF pipeline), `tg_extraction` (confirmation gate), `tg_pending` (dialog state), `tg_commands` (slash handlers). Behaviour-equivalent to the old single 2700-line file, just maintainable.
- **Storage:** ClickHouse `ReplacingMergeTree` for `lab_results`, `health_profile`, `drug_interactions_v1`, plus `chat_log`, `documents`, `training_entries`, `nutrition_log`, `glossary_terms`, `reminders`, `daily_digest`, `clinical_lessons`, `medication_events_v1` (PDC ledger).
- **LLM:** Claude Opus 4.7 primary, Sonnet 4.6 fallback — all calls via the `claude` CLI (MAX subscription or API key). Every user-driven call goes through `claude_runner.run_claude` — per-owner `threading.Lock` (max 1 inflight LLM per user) plus a daily cost log at `logs/claude-cost-YYYY-MM-DD.log`.
- **Vision OCR:** photos go through `claude -p --add-dir <dir> --permission-mode default` using the built-in Read tool — handles real-size phone photos without the `ARG_MAX` limits of inline base64. `default` permission mode prevents the model from writing to disk even if a photo carries prompt-injected instructions.
- **No external APIs for core logic** — all analytics computed in Python, only the LLM is external.
- **Multi-tenant** — isolated data per user (`owner_id` in every sort key). Every SQL filter uses `clickhouse-connect` parameter binds (`{o:String}`) or the `_own()` helper, which validates `owner_id` to digit-only before interpolation. No raw f-string SQL with owner_id anywhere.
- **Sync insert visibility** — ClickHouse client forces `async_insert=0, wait_for_async_insert=1` so the save → LLM-analysis path never hits a visibility race.

### Security posture
- **Argv hardening** — every `claude -p` invocation uses list-form `subprocess.run` with `--` before the user-supplied prompt; prompts starting with `-` or `--flag` cannot be reinterpreted as CLI options.
- **Filename hygiene** — incoming PDF/photo filenames go through `_safe_filename` (`Path().name` + strict whitelist regex + 120-char cap); 5 MB upload limit blocks zip-bomb-style PDFs.
- **Input sanitisation** — Telegram-supplied display name / username are stripped of YAML- and HTML-special chars BEFORE they reach `users.yaml`, admin-broadcast messages, or any log line.
- **Identity invariant** — `_OWNER_ID_RE = ^\d{1,19}$` is enforced; duplicate `chat_id` in `users.yaml` fail-closes the bot at startup rather than silently picking first-row-wins.
- **PHI minimisation** — `data/` and `logs/` are `chmod 0o700` at startup; log lines record message kind + length, never body text. Full text still lives in CH (owner-scoped).
- **DoS guard** — per-(owner, day) chat-message cap (500/day) before any insert; one user can't pump `chat_log` to disk exhaustion.
- **CVE floor** — `urllib3 >= 2.7.0` in `requirements.txt` (closes CVE-2026-44431 / CVE-2026-44432).

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Bot | Python, Telegram Bot API (long polling) |
| Database | ClickHouse (`ReplacingMergeTree` with `owner_id` in sort key) |
| LLM | Claude Opus 4.7 (primary) + Sonnet 4.6 (fallback) via CLI |
| PDF parsing | pdfplumber |
| Photo OCR | Claude Vision via `claude -p --add-dir` |
| Charts | matplotlib |
| PDF reports | ReportLab |

## Setup

```bash
# Clone
git clone https://github.com/petrovich-opendev/petrovich-health.git
cd petrovich-health

# Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configuration
cp users.yaml.example users.yaml   # add your Telegram username
cp .env.example .env               # add tokens

# ClickHouse tables (created automatically on first run)

# Run
python3 tg_listener.py
```

### Environment Variables

```
TELEGRAM_BOT_TOKEN=...     # from @BotFather
GITHUB_TOKEN=...           # for /feedback issues (optional)
CH_HOST=localhost           # ClickHouse
CH_PORT=8123
CH_DATABASE=health_analytics
```

## Commands (shortcuts, not required)

All features work from free chat text. Commands are optional shortcuts. The Telegram `/`-menu is **auto-synced** at startup from the canonical command list in `tg_listener.py` — it never drifts behind the code.

| Command | Description |
|---------|-------------|
| `/protocol` | Current stack + interactions + follow-up lab due-dates |
| `/ddi <drug>` | Drug-drug interactions (FDA-label citations) |
| `/adherence` | PDC over 30 days, target ≥80% |
| `/alerts` | Stale tests, trend reversals, below-PDC drugs |
| `/last` | Recent lab results |
| `/trend <marker>` | Biomarker trend chart |
| `/abnormal` | Out-of-range values |
| `/biomarkers` | All tracked markers |
| `/spc` | SPC control charts |
| `/correlations` | Cross-system analysis |
| `/search <query>` | Full-text search on labs + documents |
| `/report` | PDF report for doctor |
| `/summary` | Overall health assessment |
| `/remind` | Manage reminders (or natural-language) |
| `/eat <food>` | Log a meal |
| `/weight` | Log body weight |
| `/goal` | View current goal or run the 5-step setup (type → weight → height → age → activity). On re-entry shows the saved goal with ✏️ Change / ✅ Keep buttons; new setup uses inline keyboards for type and activity steps with ❌ Cancel on every step. |
| `/week` | Weekly review |
| `/train` | Recent workouts |
| `/progress <exercise>` | Exercise progression |
| `/glossary` | Bot knowledge base |
| `/approve <id>` | Admin: approve new user |
| `/feedback` | Report a bug (goes to public GitHub) |

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for older entries.

**May 2026 highlights:**
- `/protocol` with auto-DDI scan, `/ddi`, `/adherence` PDC dashboard, `/alerts`
- Confirmation gate (annotated preview + accept/cancel) before any lab insert
- Self-check verification layer for extraction / digest / profile
- Pre-insert deduplication guard on top of `ReplacingMergeTree`
- Diagnostician rule 8 + rolling-window stack scan (90-day lookback)
- Codebase split: `tg_listener.py` 2730 → 623 lines, 8 focused modules
- AppSec sweep: SQL parameter binds end-to-end, per-owner LLM concurrency lock with daily cost log, argv `--` separator, filename sanitiser, YAML/HTML escaping, `data/`/`logs/` chmod 700, PHI redaction, daily message cap
- `/goal` UX: re-entry surfaces the saved goal (no more silent restart), inline keyboards for type/activity steps, ❌ Cancel on every step, prior `active=true` rows deactivated on save
- `/help` synced with `setMyCommands` — `/goal`, `/weight`, `/eat`, `/week`, `/feedback` are now grouped and visible in the help body

## License

Apache 2.0

## Credits

Built with [Claude Code](https://claude.ai/code) by Anthropic.
