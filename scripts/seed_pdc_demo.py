#!/usr/bin/env python3
"""Seed 30 days of medication events for the PROMOMED CEO demo.

Creates a synthetic "demo_promomed_patient" owner_id with realistic
adherence patterns on three flagship Promomed drugs:

  * Тирзетта® (тирзепатид, weekly injection, GLP-1+GIP) — adherent
    pattern, PDC ≈ 86%. Demonstrates a patient who's "doing well".
  * Велгия® (семаглутид 2.4мг, weekly injection, anti-obesity) —
    slipping pattern, PDC ≈ 64%. Demonstrates the GLP-1 drop-off the
    pharma industry knows well; this is the kind of patient where AI
    behavioral nudges have measurable revenue impact.
  * Семальтара® (семаглутид tablet, СД2, oral) — early-discontinuation,
    PDC ≈ 45%. Demonstrates patient education gap (first oral
    semaglutide in RU — many patients don't know proper intake regime).

The CEO will see these three at /adherence and immediately recognise his
own portfolio: GLP-1 = 57% of 2025 revenue. PDC = the KPI of his
patient subscription program. The point of the demo is that the AI bot
turns this KPI from "post-hoc analytics" into a real-time intervention
surface.
"""
from __future__ import annotations
import sys
import random
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_client
from adherence import record_medication_event

DEMO_OWNER = "demo_promomed_patient"

DRUGS = [
    # (drug_name, drug_inn, dose, expected_pdc, miss_pattern)
    # miss_pattern: probability per day that user MISSES instead of takes
    ("Тирзетта", "тирзепатид", "5 мг/нед", 0.86, [("forgot", 0.10), ("na_ae", 0.04)]),
    ("Велгия", "семаглутид", "2.4 мг/нед", 0.64, [("forgot", 0.20), ("na_unavailable", 0.10), ("na_ae", 0.06)]),
    ("Семальтара", "семаглутид перорально", "7 мг/день", 0.45, [("forgot", 0.30), ("na_ae", 0.15), ("skipped", 0.10)]),
]

DAYS_BACK = 35  # window with a bit of slack so /adherence 30d gets full coverage


def seed(ch, dry_run: bool = False) -> None:
    if not dry_run:
        ch.command(
            f"ALTER TABLE medication_events_v1 DELETE WHERE owner_id = '{DEMO_OWNER}'"
        )

    rng = random.Random(20260511)
    today = datetime.now().date()

    for drug_name, drug_inn, dose, target_pdc, miss_pattern in DRUGS:
        taken = 0
        miss = 0
        # Weekly drugs (Тирзетта, Велгия) — one dose per week (Mondays).
        # Daily drug (Семальтара) — every day.
        is_weekly = "нед" in dose
        for d_offset in range(DAYS_BACK):
            d = today - timedelta(days=d_offset)
            if is_weekly and d.weekday() != 0:
                continue
            roll = rng.random()
            if roll < target_pdc:
                # Take it. Pick a random hour 8-22.
                ts = datetime.combine(d, datetime.min.time()) + timedelta(
                    hours=rng.randint(8, 22),
                    minutes=rng.randint(0, 59),
                )
                if not dry_run:
                    record_medication_event(
                        ch, DEMO_OWNER, drug_name, drug_inn, dose,
                        status="taken", source="reminder_button", ts=ts,
                    )
                taken += 1
            else:
                # Miss — distribute across configured reasons.
                acc = target_pdc
                chosen = "forgot"
                for status, pct in miss_pattern:
                    if roll < acc + pct:
                        chosen = status
                        break
                    acc += pct
                ts = datetime.combine(d, datetime.min.time()) + timedelta(
                    hours=rng.randint(8, 22), minutes=rng.randint(0, 59),
                )
                if not dry_run:
                    record_medication_event(
                        ch, DEMO_OWNER, drug_name, drug_inn, dose,
                        status=chosen, source="manual", ts=ts,
                    )
                miss += 1
        print(f"  {drug_name:12s} ({dose:12s}) — taken={taken} miss={miss} target_PDC={target_pdc:.0%}")

    if not dry_run:
        print(f"\nSeeded owner_id='{DEMO_OWNER}' — try /adherence as this user.")


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    seed(get_client(), dry_run=dry)
