"""The average calculator: how many shares move your average cost to a target.

You hold `quantity` shares at an average of `average`, and would buy more at
`price`. Buying n shares makes the new average

    (quantity * average + n * price) / (quantity + n)

so reaching `target` takes n = quantity * (average - target) / (target - price),
rounded up so the average lands at the target or a little better. A target
only lies between your average and the buying price: buying below your
average lowers it, above raises it, and it can never pass the buying price.

Figures before brokerage fees. Arithmetic, not advice: whether to add to a
position is the question the stop and the thesis in Memory are for.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AverageResult:
    buy: int
    cost: float
    new_average: float
    new_quantity: int


def shares_needed(quantity: float, average: float, target: float, price: float) -> AverageResult:
    if min(quantity, average, target, price) <= 0:
        raise ValueError("all four numbers must be above zero")
    if not (min(average, price) < target < max(average, price)):
        raise ValueError("the target average must lie between your average and the buying price")
    buy = math.ceil(quantity * (average - target) / (target - price) - 1e-9)
    new_quantity = int(quantity) + buy
    new_average = (quantity * average + buy * price) / new_quantity
    return AverageResult(buy, buy * price, new_average, new_quantity)


# --------------------------------------------------------------------------- #
# In the chat: "معايا 100 سهم متوسط 85 عايز أوصل 80 بسعر 75"
# --------------------------------------------------------------------------- #

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
_NUMBER = r"(\d+(?:[.,]\d+)?)"
_ASKS = re.compile(r"(متوسط|average|avg)", re.IGNORECASE)
_PATTERNS = {
    "quantity": re.compile(_NUMBER + r"\s*(?:سهم|اسهم|أسهم|shares?)", re.IGNORECASE),
    "average": re.compile(r"(?:متوسط(?:ي)?(?:\s+الحالي)?|average|avg)\s*(?:is|=|:|ب|بـ)?\s*"
                          + _NUMBER, re.IGNORECASE),
    "target": re.compile(r"(?:اوصل|أوصل|أنزل|انزل|أرفع|ارفع|to|target)\s*(?:ل|لـ|إلى|الى|المتوسط)?\s*"
                         r"(?:ل|لـ)?\s*" + _NUMBER, re.IGNORECASE),
    "price": re.compile(r"(?:بسعر|سعر|at|price)\s*(?:=|:)?\s*" + _NUMBER, re.IGNORECASE),
}


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def average_request(question: str) -> Optional[tuple[float, float, float, float]]:
    """(quantity, average, target, price) when the question asks the calculator."""
    text = (question or "").translate(_ARABIC_DIGITS)
    if not _ASKS.search(text):
        return None
    found = {}
    for name, pattern in _PATTERNS.items():
        match = pattern.search(text)
        if match:
            found[name] = _number(match.group(1))
    if len(found) == 4:
        return found["quantity"], found["average"], found["target"], found["price"]
    return None


def answer(values: tuple[float, float, float, float], arabic: bool) -> str:
    quantity, average, target, price = values
    try:
        r = shares_needed(quantity, average, target, price)
    except ValueError:
        return ("المتوسط المستهدف لازم يكون بين متوسطك الحالي وسعر الشراء. الشراء تحت "
                "متوسطك بينزّله، وفوقه بيرفعه، وعمره ما يعدّي سعر الشراء." if arabic else
                "The target average must lie between your current average and the buying "
                "price: buying below your average lowers it, above raises it, and it can "
                "never pass the buying price.")
    if arabic:
        return (f"محتاج تشتري {r.buy:,} سهم بسعر {price:g} (حوالي {r.cost:,.0f} جنيه). "
                f"هيبقى معاك {r.new_quantity:,} سهم بمتوسط {r.new_average:.2f}.\n"
                "الأرقام من غير عمولة السمسرة. ده حساب بس، مش نصيحة: شوف الوقف وسبب "
                "شرائك للسهم في الذاكرة قبل ما تزوّد.")
    return (f"Buy {r.buy:,} shares at {price:g} (about {r.cost:,.0f} EGP). You will hold "
            f"{r.new_quantity:,} shares at an average of {r.new_average:.2f}.\n"
            "Before brokerage fees. Arithmetic, not advice: check the stop and your "
            "thesis in Memory before adding.")
