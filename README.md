# Petrovich Health Bot

Personal health & fitness AI assistant for Telegram. Tracks lab results, analyzes workout progression, profiles nutrition down to amino acids, and learns your terminology over time.

**Bot:** [@Petrovich_bio_bot](https://t.me/Petrovich_bio_bot)

## What It Does

### Lab Results & Medical Documents
- **PDF / Photo / Text** — send lab results in any format, the bot extracts biomarkers automatically
- **Unified save → analysis** — one combined reply: saved biomarkers + LLM analysis with trends, red flags, and next steps. No "ok, saved" / "now explain" two-step dance.
- **Trend tracking** — monitor any biomarker over time with SPC control charts; unit-aware series (mmol/L vs mg/dL separated automatically)
- **Abnormal detection** — flags values outside reference ranges
- **Cross-system correlations** — links related biomarkers (liver panel, lipids, hormones, etc.)
- **Medical documents** — stores consultations, EEG, MRI, ultrasound reports for full-text search
- **Deduplication** — the same report uploaded twice (e.g. PDF + text paste) collapses into one series, not two

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

- **Storage:** ClickHouse `ReplacingMergeTree` for `lab_results` and `health_profile`, dedup keyed on `(owner_id, biomarker, collected_at, value)`. Other tables: `chat_log`, `documents`, `training_entries`, `nutrition_log`, `glossary_terms`, `reminders`, `daily_digest`, `clinical_lessons`.
- **LLM:** Claude Opus 4.7 primary, Sonnet 4.6 fallback — all calls via the `claude` CLI (MAX subscription or API key).
- **Vision OCR:** photos go through `claude -p --add-dir <dir>` using the built-in Read tool — handles real-size phone photos without the `ARG_MAX` limits of inline base64.
- **No external APIs for core logic** — all analytics computed in Python, only the LLM is external.
- **Multi-tenant** — isolated data per user (watertight `owner_id` in the sort key), shared glossary. The `_own()` helper validates `owner_id` shape to block SQL injection at the query boundary.
- **Sync insert visibility** — ClickHouse client forces `async_insert=0, wait_for_async_insert=1` so the save → LLM-analysis path never hits a visibility race.

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

All features work from free chat text. Commands are optional shortcuts:

| Command | Description |
|---------|-------------|
| `/last` | Recent lab results |
| `/trend <marker>` | Biomarker trend chart |
| `/abnormal` | Out-of-range values |
| `/biomarkers` | All tracked markers |
| `/spc` | SPC control charts |
| `/correlations` | Cross-system analysis |
| `/train` | Recent workouts |
| `/progress <exercise>` | Exercise progression |
| `/eat <food>` | Log a meal |
| `/goal` | Set fitness goal + macros |
| `/weight` | Log body weight |
| `/remind` | Manage reminders |
| `/glossary` | Bot knowledge base |
| `/approve <id>` | Admin: approve new user |
| `/report` | PDF report for doctor |
| `/search <query>` | Full-text search |
| `/summary` | Overall health assessment |

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for details of the latest changes: unified save→analysis reply, expanded clinical decision support, data-layer migration to `ReplacingMergeTree`, photo-OCR rewrite, prompt-injection hardening, reminder double-fire fix.

## License

Apache 2.0

## Credits

Built with [Claude Code](https://claude.ai/code) by Anthropic.
