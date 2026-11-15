#!/usr/bin/env python3
"""Generate a mostly-free advisors.json schedule for Mazda Service Seattle - Lee Johnson."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytz

ADVISORS = [
    ("adv_mark", "Mark Witowski"),
    ("adv_raymond", "Raymond Chow"),
    ("adv_josh", "Josh Steventon"),
    ("adv_alvin", "Alvin Wong"),
    ("adv_ryan", "Ryan Kingsbury"),
]

# Only these advisors work Saturdays; others get no Saturday slots.
SATURDAY_ADVISORS = {"Ryan Kingsbury", "Alvin Wong"}

TZ = pytz.timezone("America/Los_Angeles")
# Next full shop week: Monday–Saturday (Sunday closed).
WEEK_START = TZ.localize(datetime(2026, 11, 16, 0, 0, 0))

SLOT_START_HOUR = 8
SLOT_START_MINUTE = 0
# Last appointment start 4:30 PM (ends 5:00 PM)
SLOT_END_HOUR = 16
SLOT_END_MINUTE = 30
SLOT_MINUTES = 30
AVAILABLE_RATE = 0.80
SEED = 42  # reproducible mock calendar

OUT_PATH = Path(__file__).resolve().parent / "advisors.json"


def iter_week_days(start: datetime):
    day = start
    for _ in range(6):  # Mon–Sat
        yield day
        day += timedelta(days=1)


def iter_day_slots(day: datetime):
    cursor = day.replace(
        hour=SLOT_START_HOUR,
        minute=SLOT_START_MINUTE,
        second=0,
        microsecond=0,
    )
    end = day.replace(
        hour=SLOT_END_HOUR,
        minute=SLOT_END_MINUTE,
        second=0,
        microsecond=0,
    )
    while cursor <= end:
        yield cursor
        cursor += timedelta(minutes=SLOT_MINUTES)


def works_on_day(name: str, day: datetime) -> bool:
    """Mark/Josh/Raymond are weekday-only; Ryan and Alvin also work Saturday."""
    if day.weekday() == 5:  # Saturday
        return name in SATURDAY_ADVISORS
    return True  # Mon–Fri: all advisors


def build_schedule(rng: random.Random) -> list[dict]:
    advisors = []
    for adv_id, name in ADVISORS:
        schedule = []
        for day in iter_week_days(WEEK_START):
            if not works_on_day(name, day):
                continue  # no Saturday slots for Mark / Josh / Raymond
            for slot in iter_day_slots(day):
                status = "available" if rng.random() < AVAILABLE_RATE else "booked"
                schedule.append(
                    {
                        "datetime": slot.isoformat(),
                        "status": status,
                    }
                )
        advisors.append(
            {
                "id": adv_id,
                "name": name,
                "schedule": schedule,
            }
        )
    return advisors


def main() -> None:
    rng = random.Random(SEED)
    payload = {"advisors": build_schedule(rng)}
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    total = sum(len(a["schedule"]) for a in payload["advisors"])
    available = sum(
        1
        for a in payload["advisors"]
        for s in a["schedule"]
        if s["status"] == "available"
    )
    sat_names = sorted(
        {
            a["name"]
            for a in payload["advisors"]
            for s in a["schedule"]
            if "-11-21T" in s["datetime"]  # Saturday Nov 21 in this week
        }
    )
    print(f"Wrote {OUT_PATH}")
    print(f"Slots: {total} total, {available} available ({available / total:.0%})")
    print(f"Saturday advisors with slots: {sat_names}")


if __name__ == "__main__":
    main()
