"""Buy and sell marks on a price chart: past rule signals and today's advice.

TradingView's embeddable widget (chart_tab.py) cannot take markers from
outside: annotations need TradingView's licensed Charting Library. So the
"Signals" view draws the stock's own daily bars with TradingView's
open-source Lightweight Charts (Apache-2.0), which has markers and price
lines, and marks on them:

- the four-factor rules (the Strategy Lab's strategy), replayed day by day
  over the bars: an arrow up where all four factors agreed while out, an
  arrow down where trend or momentum broke while in;
- the operator's own indicators (indicators.signals), the signals the
  Training tab's study measures, as small circles;
- the app's past picks (memory: the plan's and the scan's), as squares;
- today's advice: the analyst team's decision on the last bar, and the stop
  and the two targets (levels.py) as horizontal lines.

Everything here is built from data up to the previous session. The marks
are drawn for the operator; nothing on the chart reaches the bot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

#: Lightweight Charts, pinned: v4's addCandlestickSeries / setMarkers API.
LIB_URL = ("https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/"
           "lightweight-charts.standalone.production.js")
#: The most marks of one kind drawn; older ones are dropped first.
MAX_MARKS = 80

UP, DOWN = "#16a34a", "#dc2626"


@dataclass(frozen=True)
class Mark:
    day: str          # ISO date: the bar the mark sits on
    side: str         # "buy" | "sell"
    source: str       # "rules" | "indicator" | "pick" | "team"
    text: str = ""

    def to_js(self) -> dict[str, Any]:
        buy = self.side == "buy"
        shape = {"rules": "arrowUp" if buy else "arrowDown", "indicator": "circle",
                 "pick": "square", "team": "arrowUp" if buy else "arrowDown"}[self.source]
        return {"time": self.day, "position": "belowBar" if buy else "aboveBar",
                "color": UP if buy else DOWN, "shape": shape, "text": self.text,
                "size": 2 if self.source == "team" else 1}


def candles(rows: Sequence[Any]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        close = float(r.close)
        out.append({"time": r.day.isoformat(),
                    "open": float(getattr(r, "open", 0) or 0) or close,
                    "high": float(r.high), "low": float(r.low), "close": close})
    return out


def rule_marks(rows: Sequence[Any]) -> list[Mark]:
    """The four-factor strategy replayed: entries and exits, as the Lab tests it."""
    from .strategy.four_factor import FourFactorParams, _trend_and_momentum

    params = FourFactorParams()
    rule = params.entry_rule
    closes = [Decimal(str(r.close)) for r in rows]
    volumes = [Decimal(str(getattr(r, "volume", 0) or 0)) for r in rows]
    marks: list[Mark] = []
    holding = False
    # The decision for day i uses data up to day i-1, as the bot does.
    for i in range(params.lookback + 1, len(rows)):
        past = closes[:i]
        if holding:
            if _trend_and_momentum(past, params):
                marks.append(Mark(rows[i].day.isoformat(), "sell", "rules", "4F out"))
                holding = False
        elif rule.blocks(past, volumes[:i]) is None:
            marks.append(Mark(rows[i].day.isoformat(), "buy", "rules", "4F in"))
            holding = True
    return marks[-MAX_MARKS:]


def indicator_marks(choices: Iterable[Any], rows: Sequence[Any]) -> list[Mark]:
    """The operator's indicators' own buy/sell signals (indicators.signals)."""
    from . import indicators as ind

    bars = ind.Bars.of(rows)
    marks: list[Mark] = []
    for choice in choices:
        if not getattr(choice, "on", True):
            continue
        name = choice.key.upper()
        for i, side in ind.signals(choice, bars):
            marks.append(Mark(rows[i].day.isoformat(), side, "indicator", name))
    marks.sort(key=lambda m: m.day)
    return marks[-MAX_MARKS:]


def pick_marks(picks: Iterable[Any], ticker: str, days: set[str]) -> list[Mark]:
    """The app's past picks for this stock, on the bars that exist."""
    out = []
    for p in picks:
        if str(p.symbol).upper().removesuffix(".CA") != ticker:
            continue
        day = p.day.isoformat()
        if day in days:
            out.append(Mark(day, "buy" if p.side == "buy" else "sell", "pick",
                            "plan" if p.source == "plan" else "scan"))
    return out[-MAX_MARKS:]


def team_mark(view: Optional[Mapping[str, Any]], last_day: str,
              today: Optional[str] = None) -> list[Mark]:
    """Today's team decision, on the last bar. An older view is not drawn as today's."""
    from datetime import date

    if str((view or {}).get("day") or "") != (today or date.today().isoformat()):
        return []
    decision = str((view or {}).get("decision") or "")
    if decision in ("buy", "add"):
        return [Mark(last_day, "buy", "team", f"Team: {decision}")]
    if decision in ("trim", "sell"):
        return [Mark(last_day, "sell", "team", f"Team: {decision}")]
    return []


def price_lines(levels: Optional[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The stop and targets as horizontal lines."""
    out = []
    for key, title, color in (("stop", "Stop", DOWN), ("target1", "T1", UP),
                              ("target2", "T2", UP)):
        try:
            value = float((levels or {}).get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            out.append({"price": value, "title": title, "color": color})
    return out


def merge(*groups: Iterable[Mark]) -> list[dict[str, Any]]:
    """Every mark as Lightweight Charts wants them: sorted by time."""
    marks = [m for group in groups for m in group]
    marks.sort(key=lambda m: (m.day, {"rules": 0, "indicator": 1, "pick": 2, "team": 3}
                              [m.source]))
    return [m.to_js() for m in marks]


def _js(value: Any) -> str:
    # "<" escaped: data can never close the script block.
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body,#c{{margin:0;height:100%;width:100%;background:{bg};color:{fg};
font:14px system-ui,sans-serif}} #legend{{position:absolute;top:8px;left:12px;z-index:3;
opacity:.85}} #c p{{padding:28px}}</style></head>
<body><div id="legend">{legend}</div><div id="c"></div>
<script src="{lib}"></script>
<script>
(function () {{
  var el = document.getElementById("c");
  if (!window.LightweightCharts) {{
    var p = document.createElement("p"); p.textContent = {failed}; el.appendChild(p); return;
  }}
  var chart = LightweightCharts.createChart(el, {{autoSize: true,
    layout: {{background: {{color: "{bg}"}}, textColor: "{fg}"}},
    grid: {{vertLines: {{color: "{grid}"}}, horzLines: {{color: "{grid}"}}}},
    timeScale: {{borderColor: "{grid}"}}, rightPriceScale: {{borderColor: "{grid}"}}}});
  var series = chart.addCandlestickSeries({{upColor: "{up}", downColor: "{down}",
    wickUpColor: "{up}", wickDownColor: "{down}", borderVisible: false}});
  series.setData({candles});
  var lines = [];
  // Called from Python (page().runJavaScript) to change marks without a reload.
  window.egxSetMarks = function (marks, priceLines) {{
    series.setMarkers(marks || []);
    lines.forEach(function (l) {{ series.removePriceLine(l); }});
    lines = (priceLines || []).map(function (l) {{
      return series.createPriceLine({{price: l.price, color: l.color, lineWidth: 1,
        lineStyle: 2, axisLabelVisible: true, title: l.title}});
    }});
  }};
  window.egxSetMarks({marks}, {lines});
  chart.timeScale().fitContent();
}})();
</script></body></html>"""


def page_html(rows: Sequence[Any], marks: list[dict[str, Any]], lines: list[dict[str, Any]],
              *, dark: bool = True, legend: str = "", failed: str = "chart unavailable"
              ) -> str:
    import html

    bg, fg, grid = ("#0f172a", "#cbd5e1", "#1e293b") if dark else ("#ffffff", "#334155",
                                                                    "#e2e8f0")
    return PAGE.format(lib=LIB_URL, bg=bg, fg=fg, grid=grid, up=UP, down=DOWN,
                       candles=_js(candles(rows)), marks=_js(marks), lines=_js(lines),
                       legend=html.escape(legend), failed=_js(failed))


def set_marks_script(marks: list[dict[str, Any]], lines: list[dict[str, Any]]) -> str:
    """JavaScript that replaces the marks on an open Signals page."""
    return f"window.egxSetMarks && window.egxSetMarks({_js(marks)}, {_js(lines)});"


def build(ticker: str, rows: Sequence[Any], *, choices: Iterable[Any] = (),
          picks: Iterable[Any] = (), levels: Optional[Mapping[str, Any]] = None,
          view: Optional[Mapping[str, Any]] = None) -> tuple[list[dict[str, Any]],
                                                             list[dict[str, Any]]]:
    """(marks, price lines) for one stock's bars, oldest first."""
    if not rows:
        return [], price_lines(levels)
    days = {r.day.isoformat() for r in rows}
    marks = merge(rule_marks(rows), indicator_marks(choices, rows),
                  pick_marks(picks, ticker, days), team_mark(view, rows[-1].day.isoformat()))
    return marks, price_lines(levels)


__all__ = ["Mark", "build", "candles", "rule_marks", "indicator_marks", "pick_marks",
           "team_mark", "price_lines", "merge", "page_html", "set_marks_script", "LIB_URL"]
