#!/usr/bin/env python3
"""
Health Diagnostician — daily L1 digest + L2 health profile.

Two modes:
  --digest   (L1) Compress today's chat into key facts (Haiku, cheap)
  --profile  (L2) Build/update cumulative health profile (Opus, thorough)

Cron:
  55 20 * * * .venv/bin/python3 diagnostician.py --digest
  0  4  * * * .venv/bin/python3 diagnostician.py --profile
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import clickhouse_connect
import requests
import yaml
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_DIR / "logs"
MSK_TZ = timezone(timedelta(hours=3))

load_dotenv(PROJECT_DIR / ".env")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

LOGS_DIR.mkdir(exist_ok=True)


def setup_logging(mode: str) -> logging.Logger:
    log_file = LOGS_DIR / f"diagnostician-{mode}-{datetime.now(MSK_TZ).strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )
    return logging.getLogger(f"diagnostician-{mode}")


def get_ch():
    return clickhouse_connect.get_client(
        host=os.getenv("CH_HOST", "localhost"),
        port=int(os.getenv("CH_PORT", "8123")),
        database="health_analytics",
        username=os.getenv("CH_USER", "default"),
        password=os.getenv("CH_PASSWORD", ""),
    )


def call_claude(prompt: str, model: str, timeout: int = 180) -> str:
    logging.info("Calling claude model=%s (%d chars)", model, len(prompt))
    result = subprocess.run(
        ["claude", "-p", "--model", model, prompt],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed (rc={result.returncode}): {result.stderr[:300]}")
    return result.stdout.strip()


def send_telegram(text: str, chat_id: str | None = None) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    target = chat_id or OWNER_CHAT_ID
    if not target:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Split long messages properly (by paragraphs, not mid-word)
    chunks = []
    current = ""
    for block in text.split("\n\n"):
        addition = block if not current else "\n\n" + block
        if len(current) + len(addition) > 3800:
            if current:
                chunks.append(current)
            current = block
        else:
            current += addition
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [text[:3800]]
    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id": target, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=10)
        if resp.status_code != 200:
            logging.error("TG send failed: %s", resp.text[:200])
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# L1: Daily Digest
# ─────────────────────────────────────────────────────────────────────────────
def run_digest(log: logging.Logger, owner_id: str = "524605979") -> int:
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    log.info("=== L1 DIGEST %s (owner=%s) ===", today, owner_id)

    ch = get_ch()

    # Get today's chat
    chat = ch.query(
        f"SELECT ts, role, text FROM chat_log "
        f"WHERE toDate(ts) = {{d:String}} AND owner_id = '{owner_id}' ORDER BY ts",
        parameters={"d": today},
    )
    if not chat.result_rows:
        log.info("No chat today — skip")
        return 0

    chat_text = "\n".join(
        f"[{row[0].strftime('%H:%M')}] {'USER' if row[1] == 'user' else 'BOT'}: {row[2][:500]}"
        for row in chat.result_rows
    )
    log.info("Chat messages today: %d", len(chat.result_rows))

    prompt = f"""Сожми диалог пользователя с медицинским ботом в краткий дайджест.

Извлеки:
1. digest — краткое резюме диалога (3-5 предложений)
2. topics — список ключевых тем (массив строк)
3. user_concerns — что беспокоит пользователя, его жалобы
4. new_info — новая информация от пользователя: препараты, образ жизни, контекст здоровья

Верни СТРОГО JSON:
{{"digest": "...", "topics": ["..."], "user_concerns": "...", "new_info": "..."}}

=== ДИАЛОГ ЗА {today} ===
{chat_text}"""

    try:
        raw = call_claude(prompt, "claude-opus-4-7", timeout=240)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"No JSON: {raw[:200]}")
        data = json.loads(m.group(0))
    except Exception as exc:
        log.error("Digest LLM failed: %s", exc)
        return 2

    ch.insert("daily_digest", [[
        date.fromisoformat(today),
        data.get("digest", ""),
        data.get("topics", []),
        data.get("user_concerns", ""),
        data.get("new_info", ""),
        "claude-opus-4-7",
        owner_id,
    ]], column_names=["date", "digest", "topics", "user_concerns", "new_info", "model", "owner_id"])

    log.info("Digest saved: %s", data.get("digest", "")[:200])
    log.info("=== L1 DONE ===")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# L2: Health Profile
# ─────────────────────────────────────────────────────────────────────────────
def run_profile(log: logging.Logger, owner_id: str = "524605979") -> int:
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    log.info("=== L2 PROFILE %s (owner=%s) ===", today, owner_id)

    ch = get_ch()
    own = f"owner_id = '{owner_id}'"

    # ── Gather ALL data ──

    # All lab results
    lab = ch.query(
        f"SELECT collected_at, category, biomarker, value, unit, "
        f"ref_low, ref_high, is_abnormal, lab_name "
        f"FROM lab_results WHERE {own} ORDER BY collected_at, biomarker"
    )
    lab_lines = []
    for r in lab.result_rows:
        flag = " [ABNORMAL]" if r[7] else ""
        ref = ""
        if r[5] is not None or r[6] is not None:
            ref = f" (ref: {r[5] or '?'}–{r[6] or '?'})"
        lab_lines.append(f"{r[0]} | {r[1]} | {r[2]}: {r[3]} {r[4]}{ref}{flag} [{r[8]}]")

    # All documents
    docs = ch.query(
        f"SELECT collected_at, doc_type, title, full_text FROM documents WHERE {own} ORDER BY collected_at"
    )
    doc_lines = []
    for r in docs.result_rows:
        doc_lines.append(f"--- {r[2]} ({r[1]}, {r[0]}) ---\n{r[3][:2000]}")

    # Recent digests (last 14 days)
    digests = ch.query(
        f"SELECT date, digest, user_concerns, new_info FROM daily_digest "
        f"WHERE {own} ORDER BY date DESC LIMIT 14"
    )
    digest_lines = []
    for r in digests.result_rows:
        parts = [f"{r[0]}: {r[1]}"]
        if r[2]:
            parts.append(f"  Concerns: {r[2]}")
        if r[3]:
            parts.append(f"  New info: {r[3]}")
        digest_lines.append("\n".join(parts))

    # Previous profile
    prev = ch.query(
        f"SELECT date, profile_text FROM health_profile WHERE {own} ORDER BY date DESC LIMIT 1"
    )
    prev_profile = ""
    if prev.result_rows:
        prev_profile = f"--- Предыдущий профиль ({prev.result_rows[0][0]}) ---\n{prev.result_rows[0][1]}"

    # Data hash — skip if nothing changed
    data_blob = f"{len(lab.result_rows)}:{len(docs.result_rows)}:{len(digests.result_rows)}"
    if lab.result_rows:
        last_lab = lab.result_rows[-1]
        data_blob += f":{last_lab[0]}:{last_lab[2]}:{last_lab[3]}"
    data_hash = hashlib.md5(data_blob.encode()).hexdigest()[:16]

    # Always rebuild — clinical lessons and digests change daily
    # Hash used only for logging, not skipping

    # SPC analysis
    from spc import compute_xmr, format_spc_for_profile, SPCPoint
    from db import query_spc_data
    spc_series = query_spc_data(owner_id)
    spc_results = []
    for bm, points in spc_series.items():
        r = compute_xmr(bm, points)
        if r:
            spc_results.append(r)
    spc_block = format_spc_for_profile(spc_results)

    log.info("Building profile: %d lab results, %d docs, %d digests, %d SPC series",
             len(lab.result_rows), len(docs.result_rows), len(digests.result_rows), len(spc_results))

    # ── Build prompt ──
    # Load clinical lessons
    lessons = ch.query(
        "SELECT condition, lesson, mechanism, evidence_level FROM clinical_lessons "
        "ORDER BY confirmed_count DESC LIMIT 30"
    )
    lessons_block = ""
    if lessons.result_rows:
        lessons_lines = [f"  [{r[3]}] {r[0]}: {r[1]}" + (f" ({r[2]})" if r[2] else "") for r in lessons.result_rows]
        lessons_block = "\n=== НАКОПЛЕННЫЕ КЛИНИЧЕСКИЕ УРОКИ ===\n" + "\n".join(lessons_lines)

    prompt = f"""Ты — персональный врач-диагност высшей категории, который непрерывно читает научную литературу с доказательной медициной. Перед тобой ПОЛНАЯ медицинская история пациента.

Твоя задача — составить актуальный Health Profile с ГЛУБОКИМ персонализированным анализом.

ПРИНЦИПЫ:
- ТОЛЬКО доказательная медицина. Уровни: [A]=мета-анализ, [B]=RCT, [C]=наблюдательные, [D]=ранние
- Давай ПРЯМЫЕ выводы: "вероятно железодефицитная анемия начальной стадии", не "рекомендую обсудить с врачом"
- Указывай конкретные значения, даты, динамику
- Ищи КОРРЕЛЯЦИИ между показателями: связи между органами, системами
- ПЕРСОНАЛИЗАЦИЯ: каждую рекомендацию проверяй через ВСЕ данные пользователя. Нет изолированных советов.
  Пример: "витамин D повышает IGF-1" → НО если АЛТ 7x нормы, печень не конвертирует D3→25-OH-D, и витамин D может не работать
- Используй SPC-анализ для выявления трендов, не только "в норме/не в норме"
- Учитывай контекст из разговоров: препараты, образ жизни, жалобы
- Помни что пациент — мужчина, 44 года, активно тренируется (массонабор)
- Учитывай ТЗТ (тестостерон энантат) и ГР (гормон роста) — они влияют на многие показатели

=== ВСЕ ЛАБОРАТОРНЫЕ АНАЛИЗЫ (хронологически) ===
{chr(10).join(lab_lines) or '(нет данных)'}

=== МЕДИЦИНСКИЕ ДОКУМЕНТЫ ===
{chr(10).join(doc_lines) or '(нет документов)'}

=== SPC-АНАЛИЗ (контрольные карты, рассчитано в Python) ===
{spc_block or '(недостаточно данных)'}

=== ДНЕВНИКИ ОБЩЕНИЯ (последние 14 дней) ===
{chr(10).join(digest_lines) or '(нет диалогов)'}

{lessons_block}

=== ПРЕДЫДУЩИЙ ПРОФИЛЬ ===
{prev_profile or '(первая генерация)'}

=== ЗАДАЧА ===
Верни СТРОГО JSON (без markdown):
{{
  "overall_status": "краткая оценка общего состояния (1 предложение)",
  "profile_text": "Подробный текст профиля (10-20 предложений). Системный анализ: кардио, кровь, печень, почки, гормоны, метаболизм, нейро. По каждой системе: текущий статус, тренды, возможные причины отклонений. Учитывай ТЗТ/ГР и тренировки.",
  "key_findings": ["находка 1 с конкретными числами", "находка 2", "..."],
  "watchlist": [
    {{"biomarker": "название", "trend": "declining/rising/borderline/stable", "last_value": "значение + единица", "last_date": "дата", "action": "что делать"}},
  ],
  "correlations": ["связь 1 между показателями", "..."],
  "missing_data": ["что не хватает для полной картины", "..."],
  "alerts": ["СРОЧНОЕ если есть, иначе пустой массив"]
}}"""

    try:
        raw = call_claude(prompt, "claude-opus-4-7", timeout=240)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"No JSON: {raw[:300]}")
        data = json.loads(m.group(0))
    except Exception as exc:
        log.error("Profile LLM failed: %s", exc)
        send_telegram(f"⚠️ <b>Diagnostician failed</b>\n\n{str(exc)[:500]}", owner_id)
        return 2

    # Delete old profile for today + owner before inserting new
    ch.command(f"ALTER TABLE health_profile DELETE WHERE date = '{today}' AND owner_id = '{owner_id}'")

    # Save to CH
    ch.insert("health_profile", [[
        date.fromisoformat(today),
        data.get("profile_text", ""),
        data.get("overall_status", ""),
        json.dumps(data.get("key_findings", []), ensure_ascii=False),
        json.dumps(data.get("watchlist", []), ensure_ascii=False),
        json.dumps(data.get("correlations", []), ensure_ascii=False),
        json.dumps(data.get("missing_data", []), ensure_ascii=False),
        json.dumps(data.get("alerts", []), ensure_ascii=False),
        data_hash,
        "claude-opus-4-7",
        owner_id,
    ]], column_names=[
        "date", "profile_text", "overall_status", "key_findings",
        "watchlist", "correlations", "missing_data", "alerts", "data_hash", "model",
        "owner_id",
    ])
    log.info("Profile saved: %s", data.get("overall_status", "")[:200])

    # ── Telegram report ──
    findings = data.get("key_findings", [])
    watchlist = data.get("watchlist", [])
    correlations = data.get("correlations", [])
    missing = data.get("missing_data", [])
    alerts = data.get("alerts", [])

    msg_parts = [
        f"🏥 <b>Health Profile — {today}</b>",
        f"\n<b>Статус:</b> {data.get('overall_status', '—')}",
    ]

    if alerts:
        msg_parts.append("\n🚨 <b>ВНИМАНИЕ:</b>")
        for a in alerts:
            msg_parts.append(f"  • {a}")

    if findings:
        msg_parts.append("\n<b>Ключевые находки:</b>")
        for f in findings[:8]:
            msg_parts.append(f"  • {f}")

    if watchlist:
        msg_parts.append("\n<b>Под наблюдением:</b>")
        for w in watchlist[:6]:
            if isinstance(w, dict):
                trend_icon = {"declining": "📉", "rising": "📈", "borderline": "⚠️", "stable": "✅"}.get(w.get("trend", ""), "•")
                msg_parts.append(f"  {trend_icon} {w.get('biomarker', '?')}: {w.get('last_value', '?')} ({w.get('last_date', '?')}) — {w.get('action', '')}")
            else:
                msg_parts.append(f"  • {w}")

    if correlations:
        msg_parts.append("\n<b>Корреляции:</b>")
        for c in correlations[:4]:
            msg_parts.append(f"  🔗 {c}")

    if missing:
        msg_parts.append("\n<b>Не хватает данных:</b>")
        for m_item in missing[:5]:
            msg_parts.append(f"  ❓ {m_item}")

    msg = "\n".join(msg_parts)
    # Escape stray < > that LLM might produce (breaks HTML parse_mode)
    import html as _html
    # Only escape content OUTSIDE our own <b>...</b> tags
    def _safe_html(text: str) -> str:
        """Escape HTML but preserve our <b> tags."""
        parts = []
        i = 0
        while i < len(text):
            if text[i:i+3] in ("<b>", ):
                end = text.find("</b>", i+3)
                if end != -1:
                    parts.append(text[i:end+4])
                    i = end + 4
                    continue
            if text[i] == "<" and not text[i:].startswith(("<b>", "</b>")):
                parts.append("&lt;")
            elif text[i] == ">" and (not parts or not parts[-1].endswith(("<b", "</b"))):
                parts.append("&gt;")
            else:
                parts.append(text[i])
            i += 1
        return "".join(parts)
    msg = _safe_html(msg)
    if send_telegram(msg, owner_id):
        log.info("Telegram profile report sent (%d chars)", len(msg))
    else:
        log.error("Telegram send failed")

    log.info("=== L2 DONE ===")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Health Diagnostician")
    parser.add_argument("--digest", action="store_true", help="L1: daily chat digest")
    parser.add_argument("--profile", action="store_true", help="L2: health profile update")
    args = parser.parse_args()

    if not args.digest and not args.profile:
        print("Specify --digest or --profile")
        return 1

    # Load all users and run for each
    users_path = PROJECT_DIR / "users.yaml"
    import yaml
    users_data = yaml.safe_load(users_path.read_text(encoding="utf-8"))
    users = users_data.get("users", [])

    if args.digest:
        log = setup_logging("digest")
        for u in users:
            cid = str(u.get("chat_id", ""))
            if cid:
                run_digest(log, owner_id=cid)
        return 0

    if args.profile:
        log = setup_logging("profile")
        for u in users:
            cid = str(u.get("chat_id", ""))
            if cid:
                run_profile(log, owner_id=cid)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
