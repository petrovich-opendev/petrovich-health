"""
Nutrition calculator — BMR, TDEE, macros, amino acid targets.
All math in Python. No LLM arithmetic.
"""
from __future__ import annotations

import json
import logging
import os
import requests
from dataclasses import dataclass

log = logging.getLogger("health-bot")

USDA_API_KEY = os.getenv("USDA_API_KEY", "DEMO_KEY")
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
USDA_FOOD_URL = "https://api.nal.usda.gov/fdc/v1/food"

# ── Activity multipliers (Mifflin-St Jeor) ──
ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}

# ── Goal caloric adjustments ──
GOAL_ADJUSTMENTS = {
    "muscle_gain": 300,    # surplus
    "fat_loss": -500,      # deficit
    "recomp": 0,           # maintenance
    "endurance": 200,      # slight surplus
    "longevity": -100,     # slight deficit (caloric restriction)
    "health": 0,           # maintenance
}

# ── Essential amino acid daily targets (mg/kg/day) ──
# Source: WHO/FAO/UNU 2007 + updated leucine research
EAA_TARGETS_MG_PER_KG = {
    "leucine": 78,       # Updated: 78mg/kg (not 39 from old RDA)
    "isoleucine": 30,
    "valine": 39,
    "lysine": 45,
    "methionine": 15,    # + cysteine combined: 22
    "threonine": 23,
    "tryptophan": 6,
    "phenylalanine": 25, # + tyrosine combined: 38
    "histidine": 15,
}

# Leucine per meal threshold for mTOR activation
LEUCINE_MEAL_THRESHOLD_G = 2.5  # minimum per meal
LEUCINE_MEAL_OPTIMAL_G = 3.0    # optimal for older adults / max MPS


def calc_bmr(weight_kg: float, height_cm: float, age: int, sex: str = "male") -> float:
    """Mifflin-St Jeor equation."""
    if sex == "male":
        return 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    return 10 * weight_kg + 6.25 * height_cm - 5 * age - 161


def calc_tdee(bmr: float, activity: str) -> float:
    return bmr * ACTIVITY_MULTIPLIERS.get(activity, 1.55)


def calc_macros(
    weight_kg: float,
    tdee: float,
    goal: str,
    on_trt: bool = False,
) -> dict:
    """Calculate macro targets."""
    adj = GOAL_ADJUSTMENTS.get(goal, 0)
    target_cal = tdee + adj

    # Protein: 2.0-2.4 g/kg on TRT/AAS, 1.6-2.0 natural
    protein_per_kg = 2.2 if on_trt else 1.8
    if goal == "fat_loss":
        protein_per_kg += 0.2  # higher protein on cut to preserve muscle

    protein_g = round(weight_kg * protein_per_kg)

    # Fat: 0.8-1.2 g/kg (hormonal health)
    fat_per_kg = 1.0
    if goal == "fat_loss":
        fat_per_kg = 0.8
    fat_g = round(weight_kg * fat_per_kg)

    # Carbs: remainder
    protein_cal = protein_g * 4
    fat_cal = fat_g * 9
    carbs_cal = max(0, target_cal - protein_cal - fat_cal)
    carbs_g = round(carbs_cal / 4)

    # Leucine target
    leucine_daily_g = round(weight_kg * EAA_TARGETS_MG_PER_KG["leucine"] / 1000, 1)

    # All EAA targets
    eaa_targets = {}
    for aa, mg_per_kg in EAA_TARGETS_MG_PER_KG.items():
        eaa_targets[aa] = round(weight_kg * mg_per_kg / 1000, 1)

    return {
        "target_calories": round(target_cal),
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
        "protein_per_kg": protein_per_kg,
        "leucine_daily_g": leucine_daily_g,
        "leucine_per_meal_g": LEUCINE_MEAL_OPTIMAL_G,
        "eaa_targets_g": eaa_targets,
        "meals_per_day": 4,  # for leucine threshold
        "protein_per_meal_g": round(protein_g / 4),
    }


def calc_weekly_rate(goal: str, weight_kg: float) -> dict:
    """Expected weekly weight change."""
    if goal == "muscle_gain":
        rate = 0.25  # 0.25 kg/week for experienced lifter on TRT
        return {"expected_kg_week": rate, "range": [0.15, 0.35],
                "note": "Выше 0.4 кг/нед — вероятно лишний жир, снизить калораж"}
    elif goal == "fat_loss":
        rate = weight_kg * 0.007  # 0.5-1% BW per week
        return {"expected_kg_week": -round(rate, 2), "range": [-round(weight_kg*0.01, 2), -round(weight_kg*0.005, 2)],
                "note": "Быстрее 1% BW/нед — теряешь мышцы, добавь калорий"}
    return {"expected_kg_week": 0, "range": [-0.1, 0.1], "note": "Поддержание"}


# ── USDA Food Lookup ──
def search_usda_food(query: str, limit: int = 5) -> list[dict]:
    """Search USDA FoodData Central for foods."""
    try:
        resp = requests.get(USDA_SEARCH_URL, params={
            "api_key": USDA_API_KEY,
            "query": query,
            "pageSize": limit,
            "dataType": ["Foundation", "SR Legacy"],
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for food in data.get("foods", []):
            nutrients = {}
            for n in food.get("foodNutrients", []):
                name = n.get("nutrientName", "")
                val = n.get("value", 0)
                unit = n.get("unitName", "")
                nutrients[name] = {"value": val, "unit": unit}
            results.append({
                "fdc_id": food.get("fdcId"),
                "description": food.get("description", ""),
                "nutrients": nutrients,
            })
        return results
    except Exception as exc:
        log.warning("USDA search failed: %s", exc)
        return []


def extract_amino_profile(nutrients: dict) -> dict:
    """Extract amino acid profile from USDA nutrient data."""
    mapping = {
        "leucine": "Leucine",
        "isoleucine": "Isoleucine",
        "valine": "Valine",
        "lysine": "Lysine",
        "methionine": "Methionine",
        "threonine": "Threonine",
        "tryptophan": "Tryptophan",
        "phenylalanine": "Phenylalanine",
        "histidine": "Histidine",
    }
    profile = {}
    for key, usda_name in mapping.items():
        for nname, ndata in nutrients.items():
            if usda_name.lower() in nname.lower():
                profile[key] = ndata.get("value", 0)
                break
        if key not in profile:
            profile[key] = 0
    return profile


def format_goal_summary(
    goal: str, weight: float, height: float, age: int, activity: str,
    bmr: float, tdee: float, macros: dict, rate: dict,
) -> str:
    """Format goal + macros for Telegram."""
    goal_labels = {
        "muscle_gain": "Набор мышечной массы",
        "fat_loss": "Снижение жира",
        "recomp": "Рекомпозиция",
        "endurance": "Выносливость",
        "longevity": "Долголетие",
        "health": "Общее здоровье",
    }
    eaa = macros.get("eaa_targets_g", {})

    lines = [
        f"<b>Цель: {goal_labels.get(goal, goal)}</b>",
        f"Вес: {weight} кг | Рост: {height} см | Возраст: {age}",
        f"Активность: {activity}",
        f"",
        f"<b>Калории:</b> {macros['target_calories']} ккал/день",
        f"  BMR: {bmr:.0f} → TDEE: {tdee:.0f} → Target: {macros['target_calories']}",
        f"",
        f"<b>Макросы:</b>",
        f"  Белок: <b>{macros['protein_g']}г</b> ({macros['protein_per_kg']}г/кг)",
        f"  Жиры: <b>{macros['fat_g']}г</b>",
        f"  Углеводы: <b>{macros['carbs_g']}г</b>",
        f"",
        f"<b>Лейцин:</b> {macros['leucine_daily_g']}г/день, ≥{macros['leucine_per_meal_g']}г за приём",
        f"  → ~{macros['protein_per_meal_g']}г белка × {macros['meals_per_day']} приёма",
    ]

    if rate.get("expected_kg_week"):
        lines.append(f"\n<b>Темп:</b> {rate['expected_kg_week']:+.2f} кг/нед")
        lines.append(f"  {rate.get('note', '')}")

    lines.append("\nПодробнее: спроси 'подробнее о протоколе'")
    return "\n".join(lines)
