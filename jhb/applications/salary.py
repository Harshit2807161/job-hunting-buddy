"""Conservative extraction of explicitly advertised annual USD base ranges."""
from __future__ import annotations

import re

_AMOUNT = r"([0-9]{2,3}(?:,[0-9]{3})|[0-9]{5,6}|[0-9]{2,3}(?:\.[0-9]+)?[kK])"
_RANGE = re.compile(r"\$\s*" + _AMOUNT + r"\s*(?:-|–|—|to)\s*\$?\s*" + _AMOUNT)


def advertised_ranges(text):
    ranges = {}
    for match in _RANGE.finditer(text):
        context = text[max(0, match.start()-180):min(len(text), match.end()+100)]
        if not re.search(r"\b(?:base|salary|pay range|compensation range)\b", context, re.I):
            continue
        if re.search(r"\b(?:per hour|hourly|per month|monthly|CAD|AUD|Canadian|Australian|total compensation|OTE|on[- ]target earnings)\b", context, re.I):
            continue
        def number(value):
            value = value.replace(",", "")
            return float(value[:-1])*1000 if value[-1].casefold() == "k" else float(value)
        lower, upper = number(match[1]), number(match[2])
        if not 20000 <= lower <= upper <= 1000000:
            continue
        ranges[(lower, upper)] = {"lower": lower, "upper": upper, "currency": "USD",
                                  "period": "annual", "evidence": context.strip()}
    return list(ranges.values())
