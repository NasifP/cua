"""EGX sectors for the scan list, and a flag when one sector leads the scan.

The mapping covers the 32 names in config/scan_universe.toml. Sector names
follow the EGX classification, shortened; chemicals and fertilizers are kept
apart from metals because they move with gas prices, not steel. It is
metadata, not an official list: check a name against the EGX website before
relying on it, and add new names here when the scan list grows. An unmapped
stock is "Other".
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional

BANKS = "Banks"
REAL_ESTATE = "Real Estate"
NBFS = "Non-bank Financial Services"
CHEMICALS = "Chemicals, Petrochemicals & Fertilizers"
METALS = "Basic Resources (Metals)"
ENERGY = "Energy"
FOOD = "Food, Beverages & Tobacco"
TELECOM = "Telecommunications"
TECH = "Technology & Payments"
CONSTRUCTION = "Contracting & Construction"
INDUSTRIALS = "Industrial Goods & Automobiles"
HEALTH = "Health Care & Pharmaceuticals"
TEXTILES = "Textiles & Durables"
TRANSPORT = "Shipping & Transport"
OTHER = "Other"

SECTORS: dict[str, str] = {
    "COMI.CA": BANKS, "CIEB.CA": BANKS, "ADIB.CA": BANKS,
    "TMGH.CA": REAL_ESTATE, "HELI.CA": REAL_ESTATE, "PHDC.CA": REAL_ESTATE,
    "EMFD.CA": REAL_ESTATE, "MASR.CA": REAL_ESTATE, "OCDI.CA": REAL_ESTATE,
    "HRHO.CA": NBFS, "CCAP.CA": NBFS, "BTFH.CA": NBFS,
    "ABUK.CA": CHEMICALS, "MFPC.CA": CHEMICALS, "SKPC.CA": CHEMICALS,
    "ESRS.CA": METALS, "EGAL.CA": METALS,
    "AMOC.CA": ENERGY,
    "EAST.CA": FOOD, "JUFO.CA": FOOD, "EFID.CA": FOOD,
    "ETEL.CA": TELECOM,
    "EFIH.CA": TECH, "FWRY.CA": TECH,
    "ORAS.CA": CONSTRUCTION,
    "SWDY.CA": INDUSTRIALS, "GBCO.CA": INDUSTRIALS,
    "ISPH.CA": HEALTH, "CLHO.CA": HEALTH, "RMDA.CA": HEALTH,
    "ORWE.CA": TEXTILES,
    "ALCN.CA": TRANSPORT,
}

#: Share of the top opportunities one sector must hold to count as momentum.
DOMINANCE = 0.5
#: Below this many opportunities, no sector can be said to dominate.
MIN_TOP = 3


def sector_of(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return SECTORS.get(s if s.endswith(".CA") else s + ".CA", OTHER)


def group(symbols: Iterable[str]) -> dict[str, list[str]]:
    """Symbols grouped by sector, sectors in order of first appearance."""
    out: dict[str, list[str]] = {}
    for s in symbols:
        out.setdefault(sector_of(s), []).append(s)
    return out


def sector_momentum(symbols: Iterable[str]) -> Optional[dict[str, Any]]:
    """The sector holding at least DOMINANCE of the top names, or None."""
    names = list(symbols)
    if len(names) < MIN_TOP:
        return None
    counts = Counter(sector_of(s) for s in names)
    sector, count = counts.most_common(1)[0]
    if sector == OTHER or count / len(names) < DOMINANCE:
        return None
    return {"sector": sector, "count": count, "of": len(names),
            "symbols": [s for s in names if sector_of(s) == sector]}
