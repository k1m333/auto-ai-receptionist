import pytest
from app import extract_time_from_speech, is_within_business_hours, answer_from_faq
from datetime import datetime
import pytz

def test_extract_time():
    assert extract_time_from_speech("tomorrow at 3 PM") == (15, 0)
    assert extract_time_from_speech("2:30 PM") == (14, 30)
    assert extract_time_from_speech("9 in the morning") == (9, 0)
    assert extract_time_from_speech("no time here") == (None, None)

def test_business_hours():
    la_tz = pytz.timezone('America/Los_Angeles')
    # Monday 8:00 AM -> should be within hours
    dt = la_tz.localize(datetime(2026, 5, 25, 8, 0))
    assert is_within_business_hours(dt) == True
    # Monday 6:00 AM -> before opening
    dt = la_tz.localize(datetime(2026, 5, 25, 6, 0))
    assert is_within_business_hours(dt) == False
    # Sunday 10:00 AM -> closed
    dt = la_tz.localize(datetime(2026, 5, 24, 10, 0))
    assert is_within_business_hours(dt) == False

def test_faq():
    # Assuming your faq.json has "hours" key
    assert "open" in answer_from_faq("What are your hours?").lower()