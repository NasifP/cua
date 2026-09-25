"""Both languages everywhere: the desktop table and the dashboard page's own."""

from __future__ import annotations

import re
import string
from pathlib import Path

from egx_advisor import i18n
from egx_advisor.backtest.lab import LabReport
from egx_advisor.strategy.filters import BuyFilter

TEMPLATE = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "index.html"


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_desktop_string_has_both_languages_with_the_same_placeholders():
    for key, (english, arabic) in i18n.STRINGS.items():
        assert english.strip() and arabic.strip(), key
        assert _fields(english) == _fields(arabic), key


def test_language_choice_and_fallbacks():
    assert i18n.name_from_env({"EGX_LANG": "EN"}) == "en"
    assert i18n.name_from_env({"EGX_LANG": "fr"}) == "ar"
    assert i18n.name_from_env({}) == "ar"
    assert i18n.tr("page.lab", lang="en") == "Strategy Lab"
    assert i18n.tr("page.lab", lang="ar") == "معمل الاستراتيجيات"
    assert i18n.tr("no.such.key", "fallback") == "fallback"
    assert i18n.tr("lab.fetching", lang="en", years=3).startswith("Fetching 3 years")
    assert i18n.is_rtl("ar") and not i18n.is_rtl("en")


def test_rule_descriptions_and_lab_verdicts_in_both_languages():
    rule = BuyFilter("rsi", {"days": 14, "above": 70})
    assert rule.describe() == "no buys when RSI(14) > 70"
    assert "RSI(14)" in rule.describe("ar") and "70" in rule.describe("ar")
    for kind in ("sma", "rsi", "momentum"):
        assert f"lab.kind.{kind}" in i18n.STRINGS and f"rule.{kind}" in i18n.STRINGS
    for key in ("verdict.synthetic", "verdict.passed", "verdict.noise", "verdict.hurts",
                "verdict.inconsistent"):
        assert key in i18n.STRINGS
    assert LabReport.verdict.__defaults__ == ("en",), "the CLI and logs keep English"


def _dashboard_tables() -> dict[str, set[str]]:
    source = TEMPLATE.read_text(encoding="utf-8")
    start = source.index("const STRINGS = {")
    block = source[start:source.index("\n};", start)]
    tables = {}
    for lang in ("en", "ar"):
        start = block.index(f"  {lang}: {{")
        end = block.index("\n  },", start)
        tables[lang] = set(re.findall(r"'([a-z_]+\.[A-Za-z_.]+)':", block[start:end]))
    return tables


def test_dashboard_page_has_both_languages_for_every_key_it_uses():
    tables = _dashboard_tables()
    assert tables["en"] == tables["ar"]
    source = TEMPLATE.read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n(?:-placeholder)?="([^"]+)"', source))
    used |= set(re.findall(r"\bt\('([a-z_]+\.[a-z_]+)'", source))
    assert used and used <= tables["en"], used - tables["en"]


def test_dashboard_keeps_the_bots_own_words_for_the_arm_confirmation():
    """The confirm press names the account in the agent's own banner, untranslated."""
    source = TEMPLATE.read_text(encoding="utf-8")
    assert "t('power.confirm') + armBanner()" in source
    assert "(lastMode && lastMode.banner) || t('power.start')" in source


def test_a_placeholder_may_be_called_key():
    assert i18n.tr("set.remove_tip", lang="en", key="GEMINI_API_KEY") == (
        "Remove the stored GEMINI_API_KEY")
