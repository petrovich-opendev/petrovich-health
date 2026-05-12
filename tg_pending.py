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


# ── Goal flow constants + keyboards ──
# Surfacing the same labels in callback data and free-text parsing keeps the
# button path and the legacy "type a digit" path in sync — users who paste a
# number after a bot restart still land on the same goal_type.
_GOAL_TYPE_LABELS = {
    "muscle_gain": "Набор массы",
    "fat_loss": "Снижение жира",
    "recomp": "Рекомпозиция",
    "endurance": "Выносливость",
    "longevity": "Долголетие",
    "health": "Здоровье",
}
_ACTIVITY_LABELS = {
    "sedentary": "Сидячий",
    "light": "Лёгкий (1-2/нед)",
    "moderate": "Средний (3-4/нед)",
    "active": "Высокий (5-6/нед)",
    "very_active": "Очень высокий",
}

_CANCEL_BUTTON = {"text": "❌ Отмена", "callback_data": "goal_cancel"}


def _goal_type_keyboard() -> dict:
    rows = [
        [{"text": f"1️⃣ {_GOAL_TYPE_LABELS['muscle_gain']}", "callback_data": "goal_type:muscle_gain"}],
        [{"text": f"2️⃣ {_GOAL_TYPE_LABELS['fat_loss']}",    "callback_data": "goal_type:fat_loss"}],
        [{"text": f"3️⃣ {_GOAL_TYPE_LABELS['recomp']}",      "callback_data": "goal_type:recomp"}],
        [{"text": f"4️⃣ {_GOAL_TYPE_LABELS['endurance']}",   "callback_data": "goal_type:endurance"}],
        [{"text": f"5️⃣ {_GOAL_TYPE_LABELS['longevity']}",   "callback_data": "goal_type:longevity"}],
        [{"text": f"6️⃣ {_GOAL_TYPE_LABELS['health']}",      "callback_data": "goal_type:health"}],
        [_CANCEL_BUTTON],
    ]
    return {"inline_keyboard": rows}


def _activity_keyboard() -> dict:
    rows = [
        [{"text": f"1️⃣ {_ACTIVITY_LABELS['sedentary']}",   "callback_data": "goal_act:sedentary"}],
        [{"text": f"2️⃣ {_ACTIVITY_LABELS['light']}",       "callback_data": "goal_act:light"}],
        [{"text": f"3️⃣ {_ACTIVITY_LABELS['moderate']}",    "callback_data": "goal_act:moderate"}],
        [{"text": f"4️⃣ {_ACTIVITY_LABELS['active']}",      "callback_data": "goal_act:active"}],
        [{"text": f"5️⃣ {_ACTIVITY_LABELS['very_active']}", "callback_data": "goal_act:very_active"}],
        [_CANCEL_BUTTON],
    ]
    return {"inline_keyboard": rows}


def _cancel_keyboard() -> dict:
    return {"inline_keyboard": [[_CANCEL_BUTTON]]}


def _existing_goal_keyboard() -> dict:
    return {"inline_keyboard": [[
        {"text": "📝 Изменить", "callback_data": "goal_change"},
        {"text": "✅ Оставить", "callback_data": "goal_keep"},
    ]]}


def _advance_to_weight(chat_id: str, owner_id: str, goal_type: str) -> None:
    """Move the dialog to the weight step. Shared by text + callback entry."""
    _pending[owner_id] = {"ts": time.time(), "action": "goal_weight",
                          "data": {"goal_type": goal_type}}
    send_message(
        chat_id,
        f"✅ Цель: <b>{_GOAL_TYPE_LABELS.get(goal_type, goal_type)}</b>\n\n"
        f"⚖️ Сколько весишь? (кг)",
        reply_markup=_cancel_keyboard(),
    )


def _save_goal_and_summarize(chat_id: str, owner_id: str, activity: str) -> None:
    """Final step: deactivate prior active goals, insert new row, send summary."""
    pending = _pending.get(owner_id) or {}
    d = pending.get("data") or {}
    _pending.pop(owner_id, None)

    bmr = calc_bmr(d["weight"], d["height"], d["age"])
    tdee = calc_tdee(bmr, activity)
    macros = calc_macros(d["weight"], tdee, d["goal_type"], on_trt=True)
    rate = calc_weekly_rate(d["goal_type"], d["weight"])

    from db import get_client
    import uuid as _uuid
    ch = get_client()
    # Deactivate prior active rows so /goal re-entry surfaces the freshest
    # one without ambiguity. CH mutations are async but eventual is fine
    # here — the new row is the one ORDER BY created_at DESC LIMIT 1 picks
    # regardless of active flag.
    try:
        ch.command(
            "ALTER TABLE goals UPDATE active = false "
            "WHERE owner_id = {o:String} AND active",
            parameters={"o": owner_id},
        )
    except Exception as exc:
        log.warning("goals deactivate prior failed (non-fatal): %s", exc)

    # created_at has DEFAULT now() — let CH fill it.
    ch.insert("goals", [[
        str(_uuid.uuid4()), owner_id, True,
        d["goal_type"], "", None, None, d["weight"], d["height"], d["age"],
        "male", activity, round(bmr), round(tdee), macros["target_calories"],
        macros["protein_g"], macros["fat_g"], macros["carbs_g"],
        macros["leucine_daily_g"], "[]",
    ]], column_names=[
        "id", "owner_id", "active",
        "goal_type", "description", "target_weight_kg", "target_date",
        "current_weight_kg", "height_cm", "age", "sex", "activity_level",
        "bmr", "tdee", "target_calories",
        "protein_g", "fat_g", "carbs_g", "leucine_target_g", "medications",
    ])

    send_message(chat_id, format_goal_summary(
        d["goal_type"], d["weight"], d["height"], d["age"],
        activity, bmr, tdee, macros, rate))


def _handle_goal_callback(chat_id: str, owner_id: str, cb_data: str) -> bool:
    """Dispatch goal_* callback_data. Returns True if handled."""
    if cb_data == "goal_cancel":
        _pending.pop(owner_id, None)
        send_message(chat_id, "❌ Отменено")
        return True

    if cb_data == "goal_keep":
        send_message(chat_id, "✅ Оставляю текущую цель")
        return True

    if cb_data == "goal_change":
        _pending[owner_id] = {"ts": time.time(), "action": "goal_type"}
        send_message(chat_id,
            "🎯 <b>Какая теперь цель?</b>",
            reply_markup=_goal_type_keyboard())
        return True

    if cb_data.startswith("goal_type:"):
        goal_type = cb_data.split(":", 1)[1]
        if goal_type not in _GOAL_TYPE_LABELS:
            return True
        _advance_to_weight(chat_id, owner_id, goal_type)
        return True

    if cb_data.startswith("goal_act:"):
        activity = cb_data.split(":", 1)[1]
        if activity not in _ACTIVITY_LABELS:
            return True
        pending = _pending.get(owner_id) or {}
        if pending.get("action") != "goal_activity":
            send_message(chat_id, "⚠️ Сначала запусти /goal — диалог не активен")
            return True
        _save_goal_and_summarize(chat_id, owner_id, activity)
        return True

    return False


def handle_goal_callback(callback: dict) -> None:
    """Telegram entry point: ack callback, resolve user, route to goal handler.

    Mirrors the shape of ``_handle_medication_callback`` so the listener loop
    can dispatch ``goal_*`` callback_data the same way as ``med_*``. Also
    clears the inline keyboard on the originating message so a stale tap
    doesn't replay the same step after the dialog has already moved on.
    """
    from tg_transport import tg_api
    from tg_users import resolve_user

    cb_id = callback.get("id", "")
    data = callback.get("data", "") or ""
    msg = callback.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    msg_id = msg.get("message_id")
    if not chat_id:
        return

    try:
        tg_api("answerCallbackQuery", callback_query_id=cb_id)
    except Exception as exc:
        log.warning("answerCallbackQuery failed: %s", exc)

    user = resolve_user({
        "from": callback.get("from", {}),
        "chat": msg.get("chat", {}),
    })
    if not user:
        send_message(chat_id, "Доступ запрещён.")
        return
    owner_id = user["owner_id"]

    # Strip the keyboard so the user can't double-tap the same step. Best
    # effort — failure here just leaves stale buttons, not data corruption.
    if msg_id is not None:
        try:
            tg_api("editMessageReplyMarkup",
                   chat_id=chat_id, message_id=msg_id,
                   reply_markup={"inline_keyboard": []})
        except Exception as exc:
            log.debug("editMessageReplyMarkup failed (non-fatal): %s", exc)

    _handle_goal_callback(chat_id, owner_id, data)


def _handle_pending(chat_id: str, owner_id: str, text: str, user: dict) -> bool:
    """Handle reply to a pending dialog. Returns True if handled."""
    pending = _pending.get(owner_id)
    if not pending:
        return False

    action = pending.get("action", "")

    # ── Feedback ──
    if action == "feedback":
        _pending.pop(owner_id, None)
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
        except Exception as exc:
            log.warning("Feedback issue create failed: %s", exc)
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
            send_message(chat_id,
                "🤔 Не понял. Жми кнопку или напиши цифру 1-6 / описание цели",
                reply_markup=_goal_type_keyboard())
            return True
        _advance_to_weight(chat_id, owner_id, goal_type)
        return True

    # ── Goal: step 2 — weight ──
    if action == "goal_weight":
        try:
            w = float(text.replace(",", ".").replace("кг", "").strip())
            pending["data"]["weight"] = w
            pending["action"], pending["ts"] = "goal_height", time.time()
            send_message(chat_id, f"✅ Вес: <b>{w} кг</b>\n\n📏 Рост? (см)",
                         reply_markup=_cancel_keyboard())
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 87",
                         reply_markup=_cancel_keyboard())
            return True

    # ── Goal: step 3 — height ──
    if action == "goal_height":
        try:
            h = float(text.replace(",", ".").replace("см", "").strip())
            pending["data"]["height"] = h
            pending["action"], pending["ts"] = "goal_age", time.time()
            send_message(chat_id, f"✅ Рост: <b>{h} см</b>\n\n🎂 Возраст?",
                         reply_markup=_cancel_keyboard())
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 183",
                         reply_markup=_cancel_keyboard())
            return True

    # ── Goal: step 4 — age ──
    if action == "goal_age":
        try:
            age = int(text.replace("лет", "").replace("год", "").strip())
            pending["data"]["age"] = age
            pending["action"], pending["ts"] = "goal_activity", time.time()
            send_message(chat_id,
                f"✅ Возраст: <b>{age}</b>\n\n🏃 Уровень активности?",
                reply_markup=_activity_keyboard())
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 44",
                         reply_markup=_cancel_keyboard())
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
            send_message(chat_id, "🤔 Жми кнопку или напиши цифру 1-5",
                         reply_markup=_activity_keyboard())
            return True

        _save_goal_and_summarize(chat_id, owner_id, activity)
        return True

    # Lazy imports for tg_commands handlers — avoid circular import at module load.
    from tg_commands import _handle_rich_command, _process_eat, handle_command

    # ── Weight ──
    if action == "weight":
        _pending.pop(owner_id, None)
        cmd_resp = handle_command(f"/weight {text}", owner_id)
        if cmd_resp and not isinstance(cmd_resp, tuple):
            send_message(chat_id, cmd_resp)
        return True

    # ── Search ──
    if action == "search":
        _pending.pop(owner_id, None)
        cmd_resp = handle_command(f"/search {text}", owner_id)
        if cmd_resp and not isinstance(cmd_resp, tuple):
            send_message(chat_id, cmd_resp)
        return True

    # ── Trend ──
    if action == "trend":
        _pending.pop(owner_id, None)
        cmd_resp = handle_command(f"/trend {text}", owner_id)
        if isinstance(cmd_resp, tuple):
            _handle_rich_command(cmd_resp, chat_id, owner_id, user)
        elif cmd_resp:
            send_message(chat_id, cmd_resp)
        return True

    # ── Eat ──
    if action == "eat":
        _pending.pop(owner_id, None)
        _process_eat(chat_id, owner_id, text)
        return True

    _pending.pop(owner_id, None)
    return False
