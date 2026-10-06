"""Strict calendar days for the observed Ashby text datepicker, without UTC."""
from __future__ import annotations

from datetime import date, datetime
import re

FORMAT = "MM/DD/YYYY"


def approved_day(value):
    if not isinstance(value, str):
        raise ValueError("Approved calendar date is unavailable or ambiguous")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
            return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        pass
    raise ValueError("Approved calendar date is unavailable or ambiguous")


def native_value(value):
    return approved_day(value).strftime("%m/%d/%Y")


def retained_day(actual, expected):
    # Raw ISO left in this textbox has not demonstrated the committed local
    # display format. Never approve it before the datepicker's blur handler.
    if not isinstance(actual, str) or not re.fullmatch(r"\d{2}/\d{2}/\d{4}", actual):
        return False
    try:
        return approved_day(actual) == approved_day(expected)
    except ValueError:
        return False
