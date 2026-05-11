"""Pending dialog state + handler for multi-step flows (goal, feedback, weight, eat, ...)."""
from __future__ import annotations

import logging
import os
import time

import requests

from nutrition import (
    calc_bmr, calc_macros, calc_tdee, calc_weekly_rate,
    format_goal_summary,
)
from tg_transport import send_message

log = logging.getLogger("health-bot")

# ── Pending actions per user (dialog state) ──
# Key: owner_id, Value: {"action": "feedback|goal_type|goal_weight|...", "data": {}}
_pending: dict[str, dict] = {}


def _handle_pending(chat_id: str, owner_id: str, text: str, user: dict) -> bool:
    """Handle reply to a pending dialog. Returns True if handled."""
    pending = _pending.get(owner_id)
    if not pending:
        return False

    action = pending.get("action", "")

    # ── Feedback ──
    if action == "feedback":
        del _pending[owner_id]
        try:
            resp = requests.post(
                "https://api.github.com/repos/petrovich-opendev/petrovich-health/issues",
                headers={
                    "Authorization": f"token {os.getenv('GITHUB_TOKEN', '')}",
                    "Content-Type": "application/json",
                },
                json={
                    "title": text[:100],
                    "body": f"**From:** {user['name']}\n\n{text}",
                    "labels": ["feedback"],
                },
                timeout=10,
            )
            if resp.status_code == 201:
                send_message(chat_id, "✅ Спасибо! Отзыв отправлен.")
            else:
                send_message(chat_id, "⚠️ Не удалось отправить. Напиши @Petrovoch_mobile")
        except Exception:
            send_message(chat_id, "⚠️ Ошибка. Напиши @Petrovoch_mobile")
        return True

    # ── Goal: step 1 — type ──
    if action == "goal_type":
        goal_map = {"1": "muscle_gain", "2": "fat_loss", "3": "recomp",
                     "4": "endurance", "5": "longevity", "6": "health"}
        text_map = {
            "набор": "muscle_gain", "масса": "muscle_gain", "массу": "muscle_gain",
            "похуд": "fat_loss", "сушк": "fat_loss", "жир": "fat_loss", "сброс": "fat_loss",
            "рекомп": "recomp",
            "выносл": "endurance", "кардио": "endurance",
            "долголет": "longevity",
            "здоров": "health",
        }
        goal_type = goal_map.get(text.strip())
        if not goal_type:
            t = text.strip().lower()
            for key, val in text_map.items():
                if key in t:
                    goal_type = val
                    break
        if not goal_type:
            send_message(chat_id, "🤔 Не понял. Напиши цифру 1-6 или опиши цель")
            return True
        _pending[owner_id] = {"ts": time.time(), "action": "goal_weight", "data": {"goal_type": goal_type}}
        goal_labels = {"muscle_gain": "Набор массы", "fat_loss": "Снижение жира",
                       "recomp": "Рекомпозиция", "endurance": "Выносливость",
                       "longevity": "Долголетие", "health": "Здоровье"}
        send_message(chat_id, f"✅ Цель: <b>{goal_labels.get(goal_type, goal_type)}</b>\n\n⚖️ Сколько весишь? (кг)")
        return True

    # ── Goal: step 2 — weight ──
    if action == "goal_weight":
        try:
            w = float(text.replace(",", ".").replace("кг", "").strip())
            pending["data"]["weight"] = w
            pending["action"], pending["ts"] = "goal_height", time.time()
            send_message(chat_id, f"✅ Вес: <b>{w} кг</b>\n\n📏 Рост? (см)")
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 87")
            return True

    # ── Goal: step 3 — height ──
    if action == "goal_height":
        try:
            h = float(text.replace(",", ".").replace("см", "").strip())
            pending["data"]["height"] = h
            pending["action"], pending["ts"] = "goal_age", time.time()
            send_message(chat_id, f"✅ Рост: <b>{h} см</b>\n\n🎂 Возраст?")
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 183")
            return True

    # ── Goal: step 4 — age ──
    if action == "goal_age":
        try:
            age = int(text.replace("лет", "").replace("год", "").strip())
            pending["data"]["age"] = age
            pending["action"], pending["ts"] = "goal_activity", time.time()
            send_message(chat_id,
                f"✅ Возраст: <b>{age}</b>\n\n"
                f"🏃 Уровень активности?\n\n"
                f"1️⃣ Сидячий (офис, мало движения)\n"
                f"2️⃣ Лёгкий (1-2 тренировки в неделю)\n"
                f"3️⃣ Средний (3-4 тренировки)\n"
                f"4️⃣ Высокий (5-6 тренировок)\n"
                f"5️⃣ Очень высокий (ежедневно + физическая работа)")
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 44")
            return True

    # ── Goal: step 5 — activity → calculate ──
    if action == "goal_activity":
        act_map = {"1": "sedentary", "2": "light", "3": "moderate", "4": "active", "5": "very_active"}
        act_text = {"сидяч": "sedentary", "офис": "sedentary", "лёг": "light", "легк": "light",
                    "средн": "moderate", "умерен": "moderate", "высок": "active", "интенс": "active",
                    "очень": "very_active", "ежедн": "very_active"}
        activity = act_map.get(text.strip())
        if not activity:
            t = text.strip().lower()
            for key, val in act_text.items():
                if key in t:
                    activity = val
                    break
        if not activity:
            send_message(chat_id, "🤔 Напиши цифру 1-5")
            return True

        d = pending["data"]
        del _pending[owner_id]

        bmr = calc_bmr(d["weight"], d["height"], d["age"])
        tdee = calc_tdee(bmr, activity)
        macros = calc_macros(d["weight"], tdee, d["goal_type"], on_trt=True)
        rate = calc_weekly_rate(d["goal_type"], d["weight"])

        from db import get_client
        import uuid as _uuid
        ch = get_client()
        ch.insert("goals", [[
            str(_uuid.uuid4()), owner_id, None, True,
            d["goal_type"], "", None, None, d["weight"], d["height"], d["age"],
            "male", activity, round(bmr), round(tdee), macros["target_calories"],
            macros["protein_g"], macros["fat_g"], macros["carbs_g"],
            macros["leucine_daily_g"], "[]",
        ]], column_names=[
            "id", "owner_id", "created_at", "active",
            "goal_type", "description", "target_weight_kg", "target_date",
            "current_weight_kg", "height_cm", "age", "sex", "activity_level",
            "bmr", "tdee", "target_calories",
            "protein_g", "fat_g", "carbs_g", "leucine_target_g", "medications",
        ])

        send_message(chat_id, format_goal_summary(
            d["goal_type"], d["weight"], d["height"], d["age"],
            activity, bmr, tdee, macros, rate))
        return True

    # Lazy imports for tg_commands handlers — avoid circular import at module load.
    from tg_commands import _handle_rich_command, _process_eat, handle_command

    # ── Weight ──
    if action == "weight":
        del _pending[owner_id]
        cmd_resp = handle_command(f"/weight {text}", owner_id)
        if cmd_resp and not isinstance(cmd_resp, tuple):
            send_message(chat_id, cmd_resp)
        return True

    # ── Search ──
    if action == "search":
        del _pending[owner_id]
        cmd_resp = handle_command(f"/search {text}", owner_id)
        if cmd_resp and not isinstance(cmd_resp, tuple):
            send_message(chat_id, cmd_resp)
        return True

    # ── Trend ──
    if action == "trend":
        del _pending[owner_id]
        cmd_resp = handle_command(f"/trend {text}", owner_id)
        if isinstance(cmd_resp, tuple):
            _handle_rich_command(cmd_resp, chat_id, owner_id, user)
        elif cmd_resp:
            send_message(chat_id, cmd_resp)
        return True

    # ── Eat ──
    if action == "eat":
        del _pending[owner_id]
        _process_eat(chat_id, owner_id, text)
        return True

    del _pending[owner_id]
    return False
