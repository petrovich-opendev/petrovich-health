# Petrovich Health Bot

Personal health & fitness AI assistant for Telegram. Tracks lab results, analyzes workout progression, profiles nutrition down to amino acids, and learns your terminology over time.

**Bot:** [@Petrovich_bio_bot](https://t.me/Petrovich_bio_bot)

## What It Does

### Lab Results & Medical Documents
- **PDF / Photo / Text** — send lab results in any format, the bot extracts biomarkers automatically
- **Trend tracking** — monitor any biomarker over time with SPC control charts
- **Abnormal detection** — flags values outside reference ranges
- **Cross-system correlations** — links related biomarkers (liver panel, lipids, hormones, etc.)
- **Medical documents** — stores consultations, EEG, MRI, ultrasound reports for full-text search

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

### LLM-Powered Q&A
- Ask any health question — the bot answers with your actual lab data, not generic advice
- Full conversation context — remembers what you discussed
- Protocol knowledge base — dosing, half-lives, interactions for TRT/peptides/supplements
- Direct answers — no "consult your doctor" copouts

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

- **Storage:** ClickHouse (lab_results, training_entries, nutrition_log, glossary_terms, ...)
- **LLM:** Claude (Opus for Q&A, Haiku for parsing) via `claude` CLI
- **No external APIs for core logic** — all analytics computed in Python
- **Multi-tenant** — isolated data per user, shared glossary

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Bot | Python, Telegram Bot API (long polling) |
| Database | ClickHouse |
| LLM | Claude (Opus 4.6 / Haiku 4.5) via CLI |
| PDF parsing | pdfplumber |
| Photo OCR | Claude Vision |
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

## License

Apache 2.0

## Credits

Built with [Claude Code](https://claude.ai/code) by Anthropic.
