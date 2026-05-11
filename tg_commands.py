"""Slash-command handlers + rich-command dispatcher (charts, reports, eat, correlations)."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import date, datetime, timedelta, timezone

from charts import render_trend_chart
from db import (
    query_abnormal,
    query_all_biomarkers,
    query_all_documents,
    query_biomarker_trend,
    query_documents_search,
    query_fulltext_search,
    query_latest_results,
    query_spc_data,
    query_summary_stats,
)
from nutrition import (
    calc_bmr, calc_macros, calc_tdee, calc_weekly_rate,
    format_goal_summary,
)
from report_pdf import generate_report
from spc import compute_xmr, format_spc_report
from tg_pending import _pending
from tg_transport import send_document_file, send_message, send_photo, send_typing

MSK_TZ = timezone(timedelta(hours=3))

log = logging.getLogger("health-bot")


# ─────────────────────────────────────────────────────────────────────────────
# /protocol formatting
# ─────────────────────────────────────────────────────────────────────────────
def _format_protocol(data: dict) -> str:
    """Render get_current_protocol() result as a Telegram HTML message."""
    stack = data.get("current_stack") or []
    due = data.get("due_dates") or []

    if not stack and not due:
        return ("💊 <b>Текущий протокол</b>\n\n"
                "Пока недостаточно данных. Расскажи в чате что принимаешь "
                "(препарат, доза, единица) — соберу стек после очередного дайджеста.")

    lines = ["💊 <b>Текущий протокол</b>"]
    if stack:
        lines.append("")
        for item in stack:
            sub = item.get("substance", "?")
            dose = item.get("dose", "")
            started = item.get("started_at")
            started_ru = ""
            if started:
                try:
                    started_ru = date.fromisoformat(started).strftime("%d.%m.%Y")
                except (TypeError, ValueError):
                    started_ru = str(started)
            head = f"  • <b>{sub}</b>"
            if dose:
                head += f" {dose}"
            if started_ru:
                head += f" — с {started_ru}"
            lines.append(head)
    else:
        lines.append("\n<i>Активный стек не распознан в дайджестах.</i>")

    if due:
        lines.append("\n<b>Рекомендованные анализы:</b>")
        for d in due:
            test = d.get("test", "?")
            due_at = d.get("due_at", "")
            try:
                due_ru = date.fromisoformat(due_at).strftime("%d.%m.%Y")
            except (TypeError, ValueError):
                due_ru = due_at or "?"
            rationale = d.get("rationale", "")
            icon = "⚠️ " if d.get("overdue") else ""
            line = f"  • {icon}<b>{test}</b> — до {due_ru}"
            if rationale:
                line += f" ({rationale})"
            lines.append(line)

    # DDI scan between active stack drugs. Surfaces here (not as a separate
    # command) so the user gets the safety check at the moment they look at
    # the stack — no extra command to remember. Skipped silently when there
    # are no known interactions for the stack — DDI table is curated, an
    # absent pair means "not in our DB", not "safe".
    if stack:
        try:
            from db import get_client
            from ddi import check_stack
            substances = [item.get("substance", "") for item in stack]
            hits = check_stack(get_client(), substances)
            if hits:
                lines.append("\n<b>⚡ Взаимодействия в стеке:</b>")
                for a, ix in hits:
                    icon = {"major": "🚨", "moderate": "⚠️"}.get(ix.severity, "ℹ️")
                    lines.append(
                        f"  {icon} <b>{a}</b> ↔ <b>{ix.drug_b_inn}</b> "
                        f"[{ix.severity}]\n     {ix.recommendation[:140]}"
                    )
        except Exception as exc:
            log.warning("DDI stack scan failed (non-fatal): %s", exc)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────
def handle_command(text: str, owner_id: str = "524605979") -> str | None:
    """Handle known commands. Returns response or None."""
    parts = text.strip().split(maxsplit=1)
    cmd_name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd_name in ("/start", "/help"):
        return (
            "🏥 <b>Health Analytics Bot</b>\n\n"
            "Отправь PDF, фото или текст анализов — я извлеку показатели и сохраню.\n\n"
            "<b>Команды:</b>\n"
            "/last — последние результаты\n"
            "/trend &lt;показатель&gt; — тренд (напр. /trend гемоглобин)\n"
            "/search &lt;текст&gt; — полнотекстовый поиск по всем анализам\n"
            "/abnormal — показатели вне нормы\n"
            "/biomarkers — список всех показателей в базе\n"
            "/spc — SPC-анализ (контрольные карты биомаркеров)\n"
            "/alerts — проактивные алерты (давность анализов, тренды)\n"
            "/protocol — текущий стек и сроки follow-up анализов\n"
            "/adherence — приверженность приёму (PDC за 30д, целевой ≥80%)\n"
            "/ddi &lt;препарат&gt; — взаимодействия лекарств (DDI)\n"
            "/correlations — корреляции по системам органов\n"
            "/remind — напоминания о приёме препаратов\n"
            "/report — PDF-отчёт для врача\n"
            "/stats — статистика базы\n"
            "/summary — общая оценка здоровья (LLM)\n"
            "/train — последние тренировки\n"
            "/progress &lt;упражнение&gt; — прогресс (напр. /progress подтягивания)\n\n"
            "<b>Ввод данных:</b>\n"
            "📎 PDF файл — парсинг и сохранение\n"
            "📸 Фото анализов — OCR распознавание\n"
            "📝 Текст анализов — автораспознавание\n"
            "🏋️ Текст тренировки — автораспознавание\n\n"
            "Любой другой текст — вопрос о здоровье.\n\n"
            "💬 Нашёл баг или есть идея? /feedback твоё сообщение"
        )

    if cmd_name == "/last":
        rows = query_latest_results(30, owner_id)
        if not rows:
            return "В базе пока нет анализов. Отправь PDF."
        current_date = None
        lines = ["<b>Последние результаты:</b>"]
        for r in rows:
            d = str(r["collected_at"])
            if d != current_date:
                current_date = d
                lines.append(f"\n<b>{d}</b> ({r.get('lab_name', '')})")
            flag = " 🔴" if r.get("is_abnormal") else ""
            ref = ""
            if r.get("ref_low") is not None or r.get("ref_high") is not None:
                ref = f" (норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')})"
            lines.append(f"  {r['biomarker']}: <b>{r['value']}</b> {r['unit']}{ref}{flag}")
        return "\n".join(lines)

    if cmd_name == "/trend":
        if not arg:
            _pending[owner_id] = {"ts": time.time(), "action": "trend"}
            return "📈 Какой показатель посмотреть? Например: <i>гемоглобин</i>, <i>АЛТ</i>, <i>железо</i>"
        return ("__trend__", arg, owner_id)

    if cmd_name == "/abnormal":
        rows = query_abnormal(owner_id=owner_id)
        if not rows:
            return "Все показатели в норме! 🎉"
        lines = ["<b>Показатели вне нормы:</b>\n"]
        for r in rows:
            ref = f"норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')}"
            lines.append(f"🔴 {r['collected_at']} | {r['biomarker']}: <b>{r['value']}</b> {r['unit']} ({ref})")
        return "\n".join(lines)

    if cmd_name == "/biomarkers":
        markers = query_all_biomarkers(owner_id)
        if not markers:
            return "В базе пока нет показателей."
        return "<b>Показатели в базе:</b>\n\n" + "\n".join(f"  • {m}" for m in markers)

    if cmd_name == "/stats":
        s = query_summary_stats(owner_id)
        return (
            f"<b>Статистика базы</b>\n\n"
            f"Записей: {s['total_records']}\n"
            f"Период: {s['earliest_date']} — {s['latest_date']}\n"
            f"Уникальных показателей: {s['unique_biomarkers']}\n"
            f"Файлов загружено: {s['unique_files']}"
        )

    if cmd_name == "/search":
        if not arg:
            _pending[owner_id] = {"ts": time.time(), "action": "search"}
            return "🔍 Что ищем? Напиши название показателя, препарата или диагноза"
        rows = query_fulltext_search(arg, owner_id=owner_id)
        doc_rows = query_documents_search(arg, owner_id=owner_id)
        if not rows and not doc_rows:
            return f"По запросу '{arg}' ничего не найдено."
        lines = [f"<b>Поиск: {arg}</b>\n"]
        if rows:
            lines.append(f"<b>Анализы ({len(rows)}):</b>")
            for r in rows:
                flag = " 🔴" if r.get("is_abnormal") else ""
                ref = ""
                if r.get("ref_low") is not None or r.get("ref_high") is not None:
                    ref = f" (норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')})"
                lines.append(f"  {r['collected_at']} | {r['biomarker']}: <b>{r['value']}</b> {r['unit']}{ref}{flag}")
        if doc_rows:
            lines.append(f"\n<b>Документы ({len(doc_rows)}):</b>")
            for d in doc_rows:
                snippet = d.get("context_snippet", "")[:150].replace("\n", " ")
                lines.append(f"  {d['collected_at']} | {d['doc_type']} | {d['title']}\n    ...{snippet}...")
        return "\n".join(lines)

    if cmd_name == "/goal":
        _pending[owner_id] = {"ts": time.time(), "action": "goal_type"}
        return (
            "🎯 <b>Какая у тебя цель?</b>\n\n"
            "1️⃣ Набор мышечной массы\n"
            "2️⃣ Снижение жира\n"
            "3️⃣ Рекомпозиция (и то и то)\n"
            "4️⃣ Выносливость\n"
            "5️⃣ Долголетие\n"
            "6️⃣ Общее здоровье"
        )

    if cmd_name == "/weight":
        if not arg:
            _pending[owner_id] = {"ts": time.time(), "action": "weight"}
            return "⚖️ Сколько весишь сегодня?"
        try:
            w = float(arg.replace(",", ".").replace("кг", "").strip())
            from db import get_client
            ch = get_client()
            ch.insert("body_log", [[
                owner_id, datetime.now(), w, None, "",
            ]], column_names=["owner_id", "ts", "weight_kg", "body_fat_pct", "notes"])
            recent = ch.query(
                "SELECT ts, weight_kg FROM body_log WHERE owner_id = {o:String} "
                "ORDER BY ts DESC LIMIT 5",
                parameters={"o": owner_id},
            )
            if len(recent.result_rows) >= 2:
                prev = recent.result_rows[1][1]
                diff = w - prev
                icon = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
                return f"⚖️ Вес: <b>{w} кг</b> ({icon} {diff:+.1f} кг)\n\n💬 Спроси <i>подробнее</i> для динамики"
            return f"⚖️ Вес: <b>{w} кг</b> — записано ✅"
        except ValueError:
            return "⚖️ Не понял. Напиши просто число, например: 87.5"

    if cmd_name == "/eat":
        if not arg:
            _pending[owner_id] = {"ts": time.time(), "action": "eat"}
            return (
                "🍽 <b>Что ты ел?</b>\n\n"
                "📸 Отправь фото тарелки\n"
                "✍️ Или напиши текстом, например:\n"
                "<i>куриная грудка 200г, рис 150г, огурец</i>"
            )
        return ("__eat__", arg, owner_id)

    if cmd_name == "/week":
        return ("__week__", owner_id)

    if cmd_name == "/spc":
        series = query_spc_data(owner_id)
        if not series:
            return "Недостаточно данных для SPC (нужно ≥2 измерения одного показателя). Загрузи ещё анализов."
        results = []
        for bm, points in series.items():
            r = compute_xmr(bm, points)
            if r:
                results.append(r)
        results.sort(key=lambda x: len(x.alerts), reverse=True)
        return format_spc_report(results)

    if cmd_name == "/alerts":
        from db import get_client
        from alerts import check_lab_fatigue, check_trend_reversals, format_alerts
        from adherence import adherence_alerts
        ch = get_client()
        items = check_lab_fatigue(ch, owner_id) + check_trend_reversals(ch, owner_id)
        rendered = format_alerts(items)
        adh = adherence_alerts(ch, owner_id)
        if adh:
            rendered = rendered + "\n\n" + "\n".join(adh)
        return rendered

    if cmd_name == "/protocol":
        from db import get_client
        from protocol import get_current_protocol
        ch = get_client()
        try:
            data = get_current_protocol(ch, owner_id)
        except Exception as exc:
            log.error("/protocol failed: %s", exc)
            return f"⚠️ Не удалось собрать протокол: {exc}"
        return _format_protocol(data)

    if cmd_name == "/ddi":
        from db import get_client
        from ddi import normalise_drug, check_interactions
        if len(parts) < 2:
            return ("Использование: /ddi &lt;препарат&gt;\n"
                    "Пример: /ddi Тирзетта  или  /ddi апалутамид\n\n"
                    "Покажу все известные взаимодействия препарата по курированной базе "
                    "(FDA labels / клинические рекомендации).")
        raw = " ".join(parts[1:])
        inn = normalise_drug(raw)
        ch = get_client()
        ix = check_interactions(ch, inn)
        if not ix:
            return (f"Для <b>{raw}</b> (МНН: <code>{inn}</code>) известных взаимодействий "
                    f"в базе нет.\n\n<i>База — курированный список ~25 топ-пар по FDA labels; "
                    f"для полного покрытия нужна DDInter/DrugBank лицензия.</i>")
        lines = [f"<b>Взаимодействия — {raw}</b>",
                 f"<i>МНН: {inn} · найдено {len(ix)} известных партнёров</i>\n"]
        for item in ix:
            lines.append(item.render())
            lines.append("")
        return "\n".join(lines).rstrip()

    if cmd_name == "/adherence":
        from db import get_client
        from adherence import adherence_summary
        ch = get_client()
        window = 30
        try:
            data = adherence_summary(ch, owner_id, window_days=window)
        except Exception as exc:
            log.error("/adherence failed: %s", exc)
            return f"⚠️ Не удалось посчитать приверженность: {exc}"
        drugs = data["drugs"]
        if not drugs:
            return ("Нет данных о приёме препаратов за последние 30 дней.\n\n"
                    "Создай напоминание с препаратом — отмечай ✅/⏭️/🤔 на каждом, "
                    "и /adherence начнёт показывать PDC.")
        lines = [f"<b>Приверженность (PDC за {window}д)</b>",
                 f"<i>Целевой PDC ≥ 80% (industry standard)</i>\n"]
        for p in drugs:
            lines.append(str(p))
            if p.miss_reasons:
                top = sorted(p.miss_reasons.items(), key=lambda kv: -kv[1])[:3]
                reasons = ", ".join(f"{k}:{v}" for k, v in top)
                lines.append(f"     причины пропусков: {reasons}")
        below = data["below_threshold"]
        if below:
            lines.append("")
            lines.append(f"<b>⚠️ Ниже целевого:</b> {', '.join(p.drug_name for p in below)}")
        return "\n".join(lines)

    if cmd_name == "/docs":
        docs = query_all_documents(owner_id=owner_id)
        if not docs:
            return "Нет сохранённых документов."
        lines = ["<b>Медицинские документы:</b>\n"]
        for d in docs:
            lines.append(f"  {d['collected_at']} | {d['doc_type']} | {d['title']} ({d['text_len']} символов)")
        return "\n".join(lines)

    if cmd_name == "/report":
        return ("__report__", owner_id)

    if cmd_name == "/correlations":
        return ("__correlations__", owner_id)

    if cmd_name == "/train":
        from db import query_recent_workouts
        rows = query_recent_workouts(10, owner_id)
        if not rows:
            return "Нет записей о тренировках. Скопируй текст тренировки из заметок — я распознаю."
        lines = ["<b>Последние тренировки:</b>\n"]
        for r in rows:
            bw = f" | {r['body_weight_kg']} кг" if r.get("body_weight_kg") else ""
            cycle = f" | Цикл #{r['cycle_number']}" if r.get("cycle_number") else ""
            lines.append(
                f"  {r['workout_date']} | <b>{r.get('training_day', '?')}</b>"
                f"{cycle}{bw}"
            )
            try:
                pj = json.loads(r.get("parsed_json", "{}"))
                ex_count = len(pj.get("exercises", []))
                if ex_count:
                    lines[-1] += f" | {ex_count} упражнений"
            except Exception:
                pass
        return "\n".join(lines)

    if cmd_name == "/progress":
        if not arg:
            return ("Укажи упражнение: <code>/progress подтягивания</code>\n"
                    "или <code>/progress тяга</code>")
        from db import query_exercise_progress
        rows = query_exercise_progress(arg, owner_id)
        if not rows:
            return f"'{arg}' не найдено в тренировках."
        lines = [f"<b>Прогресс: {arg}</b>\n"]
        for r in rows:
            try:
                pj = json.loads(r.get("parsed_json", "{}"))
                for ex in pj.get("exercises", []):
                    if arg.lower() in ex.get("name", "").lower():
                        for w in ex.get("weeks", []):
                            week_num = w.get("week", "?")
                            sets_info = []
                            for s in w.get("sets", []):
                                wt = s.get("weight_kg", "")
                                reps = s.get("reps", "")
                                eff = s.get("effort", "")
                                info = ""
                                if wt:
                                    info += f"{wt}кг"
                                if reps:
                                    info += f"x{reps}"
                                if eff:
                                    info += f" [{eff}]"
                                if info:
                                    sets_info.append(info)
                            if sets_info:
                                lines.append(
                                    f"  Цикл#{r.get('cycle_number', '?')} "
                                    f"Нед.{week_num}: {'; '.join(sets_info)}"
                                )
            except Exception:
                pass
        if len(lines) == 1:
            lines.append("  Данные найдены, но без числовой прогрессии")
        return "\n".join(lines)

    if cmd_name == "/glossary":
        from glossary import glossary_stats, search_glossary
        from db import get_client
        ch = get_client()
        if arg:
            results = search_glossary(ch, arg)
            if not results:
                return f"'{arg}' не найдено в глоссарии."
            lines = [f"<b>Глоссарий: {arg}</b>\n"]
            for r in results:
                status_icon = {"trusted": "", "verified": "", "candidate": ""}
                icon = status_icon.get(r.get("status", ""), "")
                lines.append(f"  {icon} <b>{r['term']}</b> [{r['domain']}] — {r.get('definition', '')}")
            return "\n".join(lines)
        stats = glossary_stats(ch)
        lines = [f"<b>Глоссарий бота</b> ({stats['total']} терминов)\n"]
        for domain, statuses in stats.get("domains", {}).items():
            total = sum(statuses.values())
            trusted = statuses.get("trusted", 0)
            verified = statuses.get("verified", 0)
            lines.append(f"  <b>{domain}</b>: {total} (trusted: {trusted}, verified: {verified})")
        lines.append("\nПоиск: <code>/glossary слово</code>")
        return "\n".join(lines)

    if cmd_name == "/summary":
        return None  # handled via LLM path

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Rich command dispatcher (charts, PDFs, eat, correlations)
# ─────────────────────────────────────────────────────────────────────────────
def _handle_rich_command(cmd: tuple, chat_id: str, owner_id: str, user: dict) -> None:
    """Handle commands that need to send photos/documents."""
    from tg_llm import ask_llm  # lazy — ask_llm may import heavy modules

    action = cmd[0]

    if action == "__trend__":
        arg = cmd[1]
        if not arg:
            send_message(chat_id, "Укажи показатель: /trend гемоглобин")
            return
        rows = query_biomarker_trend(arg, owner_id=owner_id)
        if not rows:
            send_message(chat_id, f"'{arg}' не найден. Попробуй /biomarkers")
            return
        lines = [f"<b>Тренд: {arg}</b>\n"]
        for r in rows:
            flag = " 🔴" if r.get("is_abnormal") else " ✅"
            ref = ""
            if r.get("ref_low") is not None or r.get("ref_high") is not None:
                ref = f" (норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')})"
            lines.append(f"{r['collected_at']} — <b>{r['value']}</b> {r['unit']}{ref}{flag}")
        send_message(chat_id, "\n".join(lines))
        if len(rows) >= 2:
            send_typing(chat_id)
            try:
                from spc import SPCPoint
                spc_points = [SPCPoint(r["collected_at"], r["value"], r.get("unit", ""),
                                       r.get("ref_low"), r.get("ref_high")) for r in rows]
                spc_r = compute_xmr(arg, spc_points)
                chart = render_trend_chart(
                    biomarker=arg,
                    dates=[r["collected_at"] for r in rows],
                    values=[r["value"] for r in rows],
                    unit=rows[0].get("unit", ""),
                    ref_low=rows[0].get("ref_low"),
                    ref_high=rows[0].get("ref_high"),
                    ucl=spc_r.ucl if spc_r else None,
                    lcl=spc_r.lcl if spc_r and spc_r.lcl > 0 else None,
                    mean=spc_r.mean if spc_r else None,
                )
                send_photo(chat_id, chart, f"{arg} — тренд")
            except Exception as exc:
                log.warning("Chart render failed: %s", exc)

    elif action == "__report__":
        send_typing(chat_id)
        send_message(chat_id, "Генерирую PDF-отчёт...")
        try:
            from db import query_health_profile
            results = query_latest_results(200, owner_id)
            docs = query_all_documents(owner_id=owner_id)
            profile = query_health_profile(owner_id)
            pdf_bytes = generate_report(
                owner_name=user.get("name", "Пациент"),
                lab_results=results,
                documents=docs,
                profile_text=profile,
            )
            filename = f"health_report_{datetime.now(MSK_TZ).strftime('%Y%m%d')}.pdf"
            send_document_file(chat_id, pdf_bytes, filename, "Медицинский отчёт")
        except Exception as exc:
            log.error("Report generation failed: %s", exc)
            send_message(chat_id, f"Ошибка генерации отчёта: {exc}")

    elif action == "__eat__":
        food_text = cmd[1] if len(cmd) > 1 else ""
        if not food_text:
            send_message(chat_id,
                "🍽 <b>Записать приём пищи</b>\n\n"
                "📸 Отправь <b>фото</b> тарелки — распознаю автоматически\n\n"
                "✍️ Или напиши что ел:\n"
                "<code>/eat куриная грудка 200г, рис 150г, огурец</code>"
            )
            return
        send_typing(chat_id)
        _process_eat(chat_id, owner_id, food_text)

    elif action == "__week__":
        send_typing(chat_id)
        send_message(chat_id, "Готовлю еженедельный отчёт...")
        answer = ask_llm(
            "Сделай еженедельный ревью моего здоровья: сравни факт питания с протоколом, "
            "динамику веса, активность за неделю, предложи корректировки. Кратко, по пунктам.",
            owner_id,
        )
        send_message(chat_id, answer)

    elif action == "__correlations__":
        send_typing(chat_id)
        _show_correlations(chat_id, owner_id)


# ── Eat processing ──
def _process_eat(chat_id: str, owner_id: str, food_text: str) -> None:
    """Process food input via LLM → structured nutrition data → CH."""
    prompt = f"""Ты — нутрициолог. Пользователь описал приём пищи. Рассчитай нутриентный состав.

ПРАВИЛА:
- Считай на указанный вес порции. Если вес не указан — бери стандартную порцию
- Все 9 незаменимых аминокислот обязательны (г)
- Микронутриенты: железо, цинк, кальций, магний, витамин D (если есть)
- DIAAS score общего приёма (0-1.5)
- Определи тип приёма: breakfast/lunch/dinner/snack

Верни СТРОГО JSON:
{{"meal_type": "lunch", "description": "краткое описание", "items": ["курица 200г", "рис 150г"],
"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "fiber_g": 0,
"leucine_g": 0, "isoleucine_g": 0, "valine_g": 0, "lysine_g": 0,
"methionine_g": 0, "threonine_g": 0, "tryptophan_g": 0, "phenylalanine_g": 0, "histidine_g": 0,
"diaas_score": 0,
"micronutrients": {{"iron_mg": 0, "zinc_mg": 0, "calcium_mg": 0, "magnesium_mg": 0}},
"warnings": ["если есть проблемы: лейцин ниже порога, плохой DIAAS, и т.д."]}}

ПРИЁМ ПИЩИ: {food_text}"""

    try:
        from claude_runner import run_claude
        result = run_claude(prompt, model="claude-opus-4-7",
                            owner_id=owner_id, timeout=90)
        if result.returncode != 0:
            send_message(chat_id, "Ошибка анализа. Попробуй описать подробнее.")
            return

        import re as _re2
        m = _re2.search(r"\{.*\}", result.stdout, _re2.DOTALL)
        if not m:
            send_message(chat_id, "Не удалось разобрать ответ. Попробуй ещё раз.")
            return
        data = json.loads(m.group(0))

        from db import get_client
        ch = get_client()
        micro_json = json.dumps(data.get("micronutrients", {}), ensure_ascii=False)
        import uuid as _uuid
        ch.insert("nutrition_log", [[
            str(_uuid.uuid4()), owner_id, datetime.now(),
            data.get("meal_type", ""), data.get("description", ""),
            data.get("calories", 0), data.get("protein_g", 0),
            data.get("fat_g", 0), data.get("carbs_g", 0), data.get("fiber_g", 0),
            data.get("leucine_g", 0), data.get("isoleucine_g", 0),
            data.get("valine_g", 0), data.get("lysine_g", 0),
            data.get("methionine_g", 0), data.get("threonine_g", 0),
            data.get("tryptophan_g", 0), data.get("phenylalanine_g", 0),
            data.get("histidine_g", 0), data.get("diaas_score", 0),
            micro_json, "text", food_text,
        ]], column_names=[
            "id", "owner_id", "ts", "meal_type", "description",
            "calories", "protein_g", "fat_g", "carbs_g", "fiber_g",
            "leucine_g", "isoleucine_g", "valine_g", "lysine_g",
            "methionine_g", "threonine_g", "tryptophan_g", "phenylalanine_g",
            "histidine_g", "diaas_score", "micronutrients", "source", "raw_input",
        ])

        leu = data.get("leucine_g", 0)
        leu_status = "✅" if leu >= 2.5 else f"⚠️ ниже порога mTOR ({leu:.1f}г < 2.5г)"
        diaas = data.get("diaas_score", 0)
        diaas_status = "✅" if diaas >= 1.0 else f"⚠️ неполный профиль ({diaas:.2f})"

        warnings = data.get("warnings", [])

        msg = (
            f"<b>{data.get('description', food_text)}</b>\n"
            f"\n{data.get('calories', 0)} ккал | "
            f"Б {data.get('protein_g', 0)}г | "
            f"Ж {data.get('fat_g', 0)}г | "
            f"У {data.get('carbs_g', 0)}г\n"
            f"\nЛейцин: {leu:.1f}г {leu_status}"
            f"\nDIAAS: {diaas:.2f} {diaas_status}"
        )
        if warnings:
            msg += "\n\n" + "\n".join(f"⚠️ {w}" for w in warnings[:3])

        today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
        totals = ch.query(
            "SELECT sum(calories), sum(protein_g), sum(leucine_g), count() "
            "FROM nutrition_log WHERE owner_id = {o:String} "
            "AND toDate(ts) = {d:String}",
            parameters={"o": owner_id, "d": today},
        )
        if totals.result_rows:
            t = totals.result_rows[0]
            msg += f"\n\n<b>Итого за день:</b> {t[0]:.0f} ккал | Белок {t[1]:.0f}г | Лейцин {t[2]:.1f}г | Приёмов: {t[3]}"

        msg += "\n\nПодробнее: спроси 'подробнее'"
        send_message(chat_id, msg)

    except Exception as exc:
        log.error("Eat processing failed: %s", exc)
        send_message(chat_id, f"Ошибка: {exc}")


# ── Correlations ──
BIOMARKER_SYSTEMS = {
    "Печень": ["АЛТ", "АСТ", "ГГТ", "Билирубин общий", "Билирубин прямой", "Билирубин непрямой", "Щелочная фосфатаза"],
    "Почки": ["Креатинин", "Мочевина", "Мочевая кислота", "Цистатин С"],
    "Железо": ["Железо сывороточное", "Ферритин", "Трансферрин", "ОЖСС", "Коэффициент насыщения трансферрина"],
    "Липиды": ["Холестерин общий", "ЛПВП", "ЛПНП", "Триглицериды"],
    "Гормоны": ["Тестостерон общий", "Тестостерон свободный", "Эстрадиол", "ТТГ", "Т3", "Т4", "Пролактин", "Кортизол", "ИФР-1", "Инсулин"],
    "Кровь (ОАК)": ["Гемоглобин", "Эритроциты", "Лейкоциты", "Тромбоциты", "Гематокрит", "СОЭ"],
    "Воспаление": ["С-реактивный белок", "СОЭ", "Лейкоциты", "Фибриноген"],
    "Метаболизм": ["Глюкоза", "HbA1c", "Белок общий", "Альбумин"],
    "Иммунология": ["АТ к фосфолипидам IgG", "АТ к фосфолипидам IgM", "Иммуноглобулин G", "Иммуноглобулин M"],
}


def _show_correlations(chat_id: str, owner_id: str) -> None:
    from db import get_client
    ch = get_client()
    result = ch.query(
        "SELECT DISTINCT biomarker FROM lab_results WHERE owner_id = {o:String}",
        parameters={"o": owner_id},
    )
    user_markers = {row[0] for row in result.result_rows}

    lines = ["<b>Корреляции по системам</b>\n"]
    found_any = False

    for system, markers in BIOMARKER_SYSTEMS.items():
        matched = [m for m in markers if any(m.lower() in um.lower() for um in user_markers)]
        if len(matched) < 2:
            continue
        found_any = True
        lines.append(f"\n<b>{system}</b> ({len(matched)} показателей):")

        for m in matched:
            latest = ch.query(
                "SELECT collected_at, value, unit, ref_low, ref_high, is_abnormal "
                "FROM lab_results WHERE owner_id = {o:String} "
                "AND biomarker ILIKE {n:String} "
                "ORDER BY collected_at DESC LIMIT 1",
                parameters={"o": owner_id, "n": f"%{m}%"},
            )
            if latest.result_rows:
                r = latest.result_rows[0]
                flag = " 🔴" if r[5] else ""
                ref = f" (норма: {r[3] or '?'}–{r[4] or '?'})" if r[3] or r[4] else ""
                lines.append(f"  {m}: <b>{r[1]}</b> {r[2]}{ref}{flag} [{r[0]}]")

    if not found_any:
        send_message(chat_id, "Недостаточно данных для корреляций. Нужно ≥2 показателя из одной системы.")
        return

    send_message(chat_id, "\n".join(lines))
