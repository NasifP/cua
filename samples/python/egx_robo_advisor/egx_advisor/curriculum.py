"""What the bot has to learn before it may speculate, and how far it has got.

The Learn tab shows this list as checkpoints. A checkpoint is ticked only by
evidence the app can count on this computer -- prices in the archive, runs in
the lab journal, picks that have aged and been measured -- never because the
model says it has learned something. When the evidence goes away (a rule that
stops beating its costs, say), the tick goes away with it.

The stages build on each other:

1. Reading the market: prices, the market's regime, the portfolio, Thndr X.
2. The operator's style: indicators, preferences, a thesis per holding.
3. Testing ideas: rules tested on real EGX history, and one that passed.
4. A track record: picks measured weeks later, beating the round-trip costs.
5. Speculation: what short-term trading needs. The paper journal, risk sizing,
   exit discipline and the loss limits are measured (paper.py,
   risk_limits.py); the rest stay locked ("needs development") until built.
6. Real money: only after three months of paper trading that beat the EGX 30
   after costs. Even then the operator presses Buy; nothing here changes that.

Passing every checkpoint is not a promise of profit. It is the least the bot
must show before it is worth trying.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .memory import Memory
from .review import review

#: Sessions of history that count as a year of prices for one stock.
YEAR_SESSIONS = 250
#: Stocks with a year of prices needed (or the whole scan list, if smaller).
PRICE_STOCKS = 20
#: Picks needed before a track record counts.
TRACK_PICKS = 30
#: Share of picks that must have gone the right way.
HIT_RATE = 0.5
#: memory.ui key prefix for the day a checkpoint was first reached.
DONE_KEY = "learn.done."
#: Paper trades for the journal and sizing checkpoints; closed ones for discipline.
PAPER_TRADES = 20
SIZED_TRADES = 10
DISCIPLINE_TRADES = 10
RULE_EXIT_SHARE = 0.9
PAPER_DAYS = 90


def round_trip_cost_pct() -> float:
    """Buy and sell costs in %, from the backtester's (pessimistic) cost model."""
    from .backtest.types import CostModel

    costs = CostModel()
    one_side = (costs.commission_rate + costs.exchange_fee_rate + costs.stamp_duty_rate
                + costs.slippage_rate)
    return float(one_side * Decimal(2) * Decimal(100))


@dataclass(frozen=True)
class Record:
    """How a set of picks did at one horizon."""

    count: int = 0
    average: float = 0.0
    right: int = 0

    @property
    def hit_rate(self) -> float:
        return self.right / self.count if self.count else 0.0


@dataclass(frozen=True)
class Evidence:
    """Everything the checkpoints are judged on, gathered once."""

    universe: int = 0
    priced: int = 0
    regime: bool = False
    portfolio: bool = False
    ticket_taught: bool = False
    stock_url: bool = False
    indicators: bool = False
    studies: int = 0
    preferences: int = 0
    theses: int = 0
    committee: int = 0
    lab_real: int = 0
    lab_passed: int = 0
    adopted: int = 0
    picks: int = 0
    horizons: Mapping[int, Record] = field(default_factory=dict)
    cost_pct: float = 0.8
    paper_opened: int = 0
    paper_closed: int = 0
    rule_exits: int = 0
    guard_active: bool = False
    #: Days since the first paper trade.
    paper_days: int = 0
    #: Paper return minus the EGX 30's over the same days, in points; None: unknown.
    paper_excess: Optional[float] = None
    #: The paper account is paused for its drawdown.
    paper_drawdown: bool = False

    def record(self, horizon: int) -> Record:
        return self.horizons.get(horizon, Record())


@dataclass(frozen=True)
class Progress:
    have: float
    need: float
    #: How to show the numbers: "count", "flag" or "pct".
    unit: str = "count"
    #: For a track record: the share of picks that went the right way.
    hit_rate: Optional[float] = None
    #: Any further condition, e.g. no drawdown pause.
    ok: bool = True

    @property
    def done(self) -> bool:
        return (self.have >= self.need and self.ok
                and (self.hit_rate is None or self.hit_rate >= HIT_RATE))


Check = Callable[[Evidence], Progress]


@dataclass(frozen=True)
class Checkpoint:
    key: str
    stage: int
    #: (English, Arabic)
    title: tuple[str, str]
    goal: tuple[str, str]
    #: None: the app cannot do this yet; it stays locked until it is built.
    check: Optional[Check] = None


def _flag(value: bool) -> Progress:
    return Progress(1.0 if value else 0.0, 1.0, "flag")


def _edge(horizon: int) -> Check:
    """Average move after `horizon` sessions beats the costs, on enough picks, mostly right."""
    def check(e: Evidence) -> Progress:
        r = e.record(horizon)
        if r.count < TRACK_PICKS:
            # Too few picks to judge: show how far the sample is from counting.
            return Progress(r.count, TRACK_PICKS)
        return Progress(round(r.average, 2), round(e.cost_pct, 2), "pct", r.hit_rate)
    return check


def _discipline(e: Evidence) -> Progress:
    if e.paper_closed < DISCIPLINE_TRADES:
        return Progress(e.paper_closed, DISCIPLINE_TRADES)
    return Progress(round(e.rule_exits / e.paper_closed, 3), RULE_EXIT_SHARE, "share")


def _beat_index(e: Evidence) -> Progress:
    if e.paper_days < PAPER_DAYS or e.paper_excess is None:
        # Too early, or no EGX 30 prices to compare with: not judged.
        return Progress(e.paper_days, PAPER_DAYS, "days", ok=e.paper_excess is not None)
    return Progress(round(e.paper_excess, 2), 0.01, "excess", ok=not e.paper_drawdown)


STAGES: tuple[tuple[str, str], ...] = (
    ("Reading the market", "قراءة السوق"),
    ("Your style", "أسلوبك في التداول"),
    ("Testing ideas", "اختبار الأفكار"),
    ("A real track record", "سجل حقيقي"),
    ("Speculation", "المضاربة"),
    ("Real money", "فلوس حقيقية"),
)

CHECKPOINTS: tuple[Checkpoint, ...] = (
    # 1. Reading the market
    Checkpoint(
        "prices", 1,
        ("A year of prices for the scan list", "أسعار سنة كاملة لقائمة الأسهم"),
        ("At least a year of daily prices kept for 20 stocks of the scan list (or all of "
         "them, if fewer). Run a scan or a Lab test and the archive fills itself.",
         "سعر يومي لمدة سنة على الأقل محفوظ لـ 20 سهم من قائمة البحث (أو كلها لو أقل). "
         "شغّل بحث عن فرص أو اختبار في المعمل والأرشيف بيتملي لوحده."),
        lambda e: Progress(e.priced, min(PRICE_STOCKS, e.universe) or PRICE_STOCKS)),
    Checkpoint(
        "regime", 1,
        ("The market's mood (regime)", "مزاج السوق (Regime)"),
        ("The bot has judged the market regime at least once: calm, risky or falling, from "
         "the EGX 30 and the news. Start the bot once.",
         "البوت حكم على حالة السوق مرة على الأقل: هادي، خطر، أو نازل، من EGX 30 والأخبار. "
         "شغّل البوت مرة."),
        lambda e: _flag(e.regime)),
    Checkpoint(
        "portfolio", 1,
        ("Reading your portfolio", "قراءة محفظتك"),
        ("The portfolio has been read from Thndr X: positions, cash and unsettled cash.",
         "المحفظة اتقرت من Thndr X: الأسهم والكاش والكاش اللي لسه ما اتسواش."),
        lambda e: _flag(e.portfolio)),
    Checkpoint(
        "ticket", 1,
        ("The Thndr X order ticket", "تذكرة الأوامر في Thndr X"),
        ("The quantity and price boxes of the order ticket are taught (Training tab).",
         "خانتين الكمية والسعر في تذكرة الأمر متعلَّمين (صفحة التدريب)."),
        lambda e: _flag(e.ticket_taught)),
    Checkpoint(
        "stock_page", 1,
        ("Opening a stock in Thndr X", "فتح صفحة السهم في Thndr X"),
        ("The address of a stock page is learned: open any stock in Thndr X once.",
         "عنوان صفحة السهم متعلَّم: افتح أي سهم في Thndr X مرة."),
        lambda e: _flag(e.stock_url)),
    # 2. Your style
    Checkpoint(
        "indicators", 2,
        ("Your indicators", "مؤشراتك"),
        ("Your indicator set is saved in the Training tab.",
         "مجموعة المؤشرات بتاعتك محفوظة في صفحة التدريب."),
        lambda e: _flag(e.indicators)),
    Checkpoint(
        "studies", 2,
        ("Indicators studied on the EGX", "دراسة المؤشرات على البورصة المصرية"),
        ("Three indicator studies: what actually followed each signal on EGX stocks "
         "(Training tab, \"Study on the EGX\").",
         "3 دراسات مؤشرات: إيه اللي حصل فعلاً بعد كل إشارة على أسهم البورصة "
         "(صفحة التدريب، \"ادرس على البورصة\")."),
        lambda e: Progress(e.studies, 3)),
    Checkpoint(
        "preferences", 2,
        ("What you want", "إنت عايز إيه"),
        ("At least one preference in memory: risk, sectors, how long you hold. Say "
         "\"remember that ...\" in the chat or add it in the Memory tab.",
         "تفضيل واحد على الأقل في الذاكرة: المخاطرة، القطاعات، مدة الاحتفاظ. قول "
         "\"افتكر إن ...\" في الشات أو ضيفه من صفحة الذاكرة."),
        lambda e: Progress(e.preferences, 1)),
    Checkpoint(
        "theses", 2,
        ("Why you hold what you hold", "ليه شايل الأسهم دي"),
        ("Three theses in memory: why a stock is held, its risks and when to exit.",
         "3 أسباب (Thesis) في الذاكرة: ليه السهم متشال، مخاطره، وإمتى نخرج منه."),
        lambda e: Progress(e.theses, 3)),
    # 3. Testing ideas
    Checkpoint(
        "lab_runs", 3,
        ("Rules tested on real history", "قواعد اتجربت على تاريخ حقيقي"),
        ("Five Strategy Lab runs on real EGX prices (not the synthetic sample).",
         "5 اختبارات في المعمل على أسعار البورصة الحقيقية (مش العينة التجريبية)."),
        lambda e: Progress(e.lab_real, 5)),
    Checkpoint(
        "lab_pass", 3,
        ("A rule that passed", "قاعدة نجحت"),
        ("One rule passed the Lab on real prices, after costs.",
         "قاعدة واحدة نجحت في المعمل على أسعار حقيقية، بعد المصاريف."),
        lambda e: Progress(e.lab_passed, 1)),
    Checkpoint(
        "adopted", 3,
        ("A rule in use", "قاعدة شغّالة"),
        ("A passed rule is adopted, so the bot applies it before every buy.",
         "قاعدة ناجحة اتعملها Adopt، والبوت بيطبقها قبل أي شرا."),
        lambda e: Progress(e.adopted, 1)),
    Checkpoint(
        "committee", 3,
        ("The team's opinions", "رأي الفريق"),
        ("The analyst team (committee) has given its decision on five stocks.",
         "فريق المحللين (اللجنة) قال قراره في 5 أسهم."),
        lambda e: Progress(e.committee, 5)),
    # 4. A real track record
    Checkpoint(
        "picks", 4,
        ("Picks written down", "الاختيارات متسجلة"),
        (f"{TRACK_PICKS} picks from the scan or the plan, kept with their price, so they "
         "can be judged later.",
         f"{TRACK_PICKS} اختيار من البحث أو الخطة متسجلين بسعرهم، علشان نحكم عليهم بعدين."),
        lambda e: Progress(e.picks, TRACK_PICKS)),
    Checkpoint(
        "measured", 4,
        ("Picks judged after a month", "الاختيارات اتحكم عليها بعد شهر"),
        (f"{TRACK_PICKS} picks are 20 sessions old and measured.",
         f"{TRACK_PICKS} اختيار عدّى عليهم 20 جلسة واتقاسوا."),
        lambda e: Progress(e.record(20).count, TRACK_PICKS)),
    Checkpoint(
        "edge_20", 4,
        ("Beating the costs over a month", "بيكسب أكتر من المصاريف في شهر"),
        ("After 20 sessions, the picks' average move is larger than a buy and a sell cost, "
         f"and at least {HIT_RATE:.0%} went the right way ({TRACK_PICKS} picks or more).",
         "بعد 20 جلسة، متوسط حركة الاختيارات أكبر من مصاريف الشرا والبيع، وعلى الأقل "
         f"{HIT_RATE:.0%} منهم طلعوا صح ({TRACK_PICKS} اختيار أو أكتر)."),
        _edge(20)),
    Checkpoint(
        "edge_60", 4,
        ("Still beating them after three months", "لسه بيكسب بعد 3 شهور"),
        ("The same, 60 sessions after each pick: the edge lasted.",
         "نفس الكلام بعد 60 جلسة من كل اختيار: الميزة استمرت."),
        _edge(60)),
    # 5. Speculation: not built yet
    Checkpoint(
        "intraday", 5,
        ("Prices during the session", "الأسعار أثناء الجلسة"),
        ("Minute-by-minute prices. Speculation lives inside the day; today the app sees only "
         "daily closes. Needs a live or delayed intraday source for the EGX.",
         "أسعار دقيقة بدقيقة. المضاربة بتحصل جوه اليوم، والبرنامج دلوقتي شايف سعر الإقفال "
         "بس. محتاج مصدر أسعار لحظية أو متأخرة شوية للبورصة المصرية.")),
    Checkpoint(
        "order_book", 5,
        ("Liquidity and the order book", "السيولة ودفتر الأوامر"),
        ("Bid/ask spread and depth, so a trade is sized to what can be bought and sold "
         "back without moving the price.",
         "الفرق بين سعر العرض والطلب وعمق السوق، علشان حجم الصفقة يبقى على قد اللي "
         "ينفع يتشرى ويتباع من غير ما السعر يتحرك.")),
    Checkpoint(
        "sizing", 5,
        ("Position size from risk", "حجم الصفقة من المخاطرة"),
        (f"{SIZED_TRADES} paper trades sized from their stop distance (ATR), so one loss is a "
         "fixed small share of the account (Settings: risk per paper trade, 1%).",
         f"{SIZED_TRADES} صفقات تجريبية حجمها اتحسب من المسافة للـ Stop (ATR)، علشان الخسارة "
         "الواحدة تبقى نسبة صغيرة ثابتة من الحساب (الإعدادات: المخاطرة في الصفقة، 1%)."),
        lambda e: Progress(e.paper_opened, SIZED_TRADES)),
    Checkpoint(
        "paper", 5,
        ("Paper trading journal", "دفتر تداول تجريبي"),
        (f"{PAPER_TRADES} virtual trades closed and written down: entry, stop, target, exit, "
         "fees and result. Every buy the bot's plan wants becomes one (Paper trading page).",
         f"{PAPER_TRADES} صفقة وهمية اتقفلت واتسجلت: الدخول، الـ Stop، الهدف، الخروج، "
         "المصاريف والنتيجة. كل شرا بتطلبه خطة البوت بيبقى صفقة (صفحة التداول التجريبي)."),
        lambda e: Progress(e.paper_closed, PAPER_TRADES)),
    Checkpoint(
        "exits", 5,
        ("Exit discipline", "الالتزام بالخروج"),
        (f"At least {RULE_EXIT_SHARE:.0%} of {DISCIPLINE_TRADES} or more closed paper trades "
         "left at their stop or target, not closed by hand or held \"until it comes back\".",
         f"على الأقل {RULE_EXIT_SHARE:.0%} من {DISCIPLINE_TRADES} صفقات تجريبية مقفولة أو أكتر "
         "خرجوا عند الـ Stop أو الهدف، مش اتقفلوا باليد أو فضلوا مستنيين \"لحد ما ترجع\"."),
        _discipline),
    Checkpoint(
        "loss_limits", 5,
        ("Daily loss limit and drawdown", "حد خسارة يومي وأقصى تراجع"),
        ("A daily loss limit and a maximum drawdown that halt the bot by themselves, "
         "checked at least once on the real portfolio or the paper account (Settings).",
         "حد أقصى للخسارة في اليوم وحد لأقصى تراجع في الحساب، ولو اتعدوا البوت يقف "
         "لوحده. بيتعلّم لما يتفحصوا مرة على الأقل على المحفظة أو الحساب التجريبي."),
        lambda e: _flag(e.guard_active)),
    Checkpoint(
        "walk_forward", 5,
        ("Tested on unseen years", "اختبار على سنين ما شافهاش"),
        ("Walk-forward testing: tune on some years, then judge on later years the rule "
         "never saw. Stops a lucky fit from passing as skill.",
         "اختبار Walk-forward: نظبط القاعدة على سنين، ونحكم عليها بسنين بعدها ما شافتهاش. "
         "ده بيمنع إن الحظ يبان كأنه شطارة.")),
    Checkpoint(
        "egx_rules", 5,
        ("The exchange's own rules", "قواعد البورصة نفسها"),
        ("Daily price limits, trading halts, session times and T+ settlement, so a "
         "same-day exit is only planned when the rules allow it.",
         "حدود الحركة اليومية للسعر، إيقاف التداول، مواعيد الجلسات، وتسوية T+، علشان "
         "الخروج في نفس اليوم يتخطط بس لما القواعد تسمح.")),
    # 6. Real money
    Checkpoint(
        "paper_3m", 6,
        ("Three months of paper profit", "3 شهور مكسب تجريبي"),
        ("Three months of paper trading that beat the EGX 30 after all costs, without "
         "breaking the loss limits. Only then is real money worth trying, and you still "
         "press Buy yourself.",
         "3 شهور تداول تجريبي كسبوا أكتر من EGX 30 بعد كل المصاريف، من غير ما يعدّوا حدود "
         "الخسارة. ساعتها بس يستاهل نجرب بفلوس حقيقية، وبرضه إنت اللي بتدوس Buy."),
        _beat_index),
)


@dataclass(frozen=True)
class Status:
    checkpoint: Checkpoint
    #: "done", "learning" or "locked"
    state: str
    progress: Optional[Progress] = None
    #: The day the checkpoint was first reached (ISO), when done.
    since: str = ""


def evaluate(evidence: Evidence, remembered: Mapping[str, str] | None = None
             ) -> list[Status]:
    """Every checkpoint's state. `remembered` maps key -> day first reached."""
    remembered = remembered or {}
    out = []
    for cp in CHECKPOINTS:
        if cp.check is None:
            out.append(Status(cp, "locked"))
            continue
        progress = cp.check(evidence)
        state = "done" if progress.done else "learning"
        out.append(Status(cp, state, progress,
                          remembered.get(cp.key, "") if state == "done" else ""))
    return out


def summary(statuses: Sequence[Status]) -> tuple[int, int]:
    """(done, all) over the checkpoints the app can measure today."""
    measured = [s for s in statuses if s.state != "locked"]
    return sum(1 for s in measured if s.state == "done"), len(measured)


def remember(memory: Memory, statuses: Sequence[Status], today: str) -> dict[str, str]:
    """Keep the day each checkpoint was first reached; forget it when it is lost."""
    days = {}
    for s in statuses:
        key = DONE_KEY + s.checkpoint.key
        saved = memory.ui_get(key) or ""
        if s.state == "done":
            if not saved:
                memory.ui_set(key, today)
                saved = today
            days[s.checkpoint.key] = saved
        elif saved:
            memory.ui_set(key, "")
    return days


# ------------------------------------------------------------------ evidence


def _snapshot(bus: Any, key: str) -> Any:
    try:
        entry = bus.get(key) if bus is not None else None
    except Exception:  # noqa: BLE001 - no bus: nothing learned from it
        return None
    return entry["payload"] if entry else None


def gather(memory: Memory, archive: Any = None, bus: Any = None,
           universe: Sequence[str] = (), config_dir: Optional[Path] = None,
           today: Any = None) -> Evidence:
    """Count what the app has on this computer. Off the UI thread: it reads files."""
    from datetime import date, timedelta

    from . import browse

    today = today or date.today()
    config_dir = config_dir or Path("config")

    priced = 0
    if archive is not None:
        since = today - timedelta(days=400)
        for symbol in universe:
            try:
                if len(archive.series(symbol, since)) >= YEAR_SESSIONS:
                    priced += 1
            except Exception:  # noqa: BLE001 - an unreadable symbol is not priced
                pass

    adopted = 0
    try:
        from .strategy.filters import load_rules

        adopted = len(load_rules(config_dir / "rules.toml"))
    except Exception:  # noqa: BLE001 - a broken rules file adopts nothing
        adopted = 0

    runs = memory.lab_runs(limit=1000)
    notes = memory.notes()
    picks = memory.picks()

    def closes(symbol: str) -> list:
        if archive is None:
            return []
        return [(r.day, float(r.close)) for r in archive.series(symbol)]

    horizons: dict[int, Record] = {}
    for result in review(picks, closes) if picks else []:
        for horizon, move in result.moves.items():
            if move is None:
                continue
            r = horizons.get(horizon, Record())
            horizons[horizon] = Record(r.count + 1, r.average + move, r.right + (move > 0))
    horizons = {h: Record(r.count, r.average / r.count, r.right)
                for h, r in horizons.items() if r.count}

    views = _snapshot(bus, "team_views") or {}
    paper = _paper_evidence(memory, archive, today)
    guarded = paper.pop("guarded")
    return Evidence(
        universe=len(universe),
        priced=priced,
        regime=bool(_snapshot(bus, "regime")),
        portfolio=bool(_snapshot(bus, "portfolio")),
        ticket_taught=(config_dir / "thndr.ticket.toml").exists(),
        stock_url=bool(memory.ui_get(browse.STOCK_URL_KEY)),
        indicators=bool(memory.ui_get(Memory.INDICATORS_KEY)),
        studies=len(memory.indicator_studies(latest_only=True)),
        preferences=sum(1 for n in notes if n.kind == "preference"),
        theses=sum(1 for n in notes if n.kind == "thesis"),
        committee=len(views) if isinstance(views, dict) else 0,
        lab_real=sum(1 for r in runs if not r.synthetic),
        lab_passed=sum(1 for r in runs if r.passed and not r.synthetic),
        adopted=adopted,
        picks=len(picks),
        horizons=horizons,
        cost_pct=round_trip_cost_pct(),
        guard_active=bool(_snapshot(bus, "loss_guard")) or guarded,
        **paper,
    )


def _paper_evidence(memory: Memory, archive: Any, today: Any) -> dict[str, Any]:
    """What the paper journal (paper.py), kept next to the memory, shows."""
    from .paper import RULE_EXITS, PaperBook

    book = PaperBook.from_env(memory.path, os.environ)
    trades = book.trades()
    closed = [t for t in trades if t.status == "closed"]
    guard = book.guard()
    out: dict[str, Any] = {
        "paper_opened": len(trades), "paper_closed": len(closed),
        "rule_exits": sum(1 for t in closed if t.exit_reason in RULE_EXITS),
        "paper_drawdown": "drawdown" in (guard.get("fired") or {}),
        "guarded": bool(guard), "paper_days": 0, "paper_excess": None,
    }
    curve = book.equity_curve()
    if not trades or not curve:
        return out
    first = min(t.opened for t in trades)
    out["paper_days"] = (today - first).days
    if archive is None:
        return out
    index = [r for r in archive.series("^CASE30", first) if r.day <= curve[-1][0]]
    if len(index) >= 2 and float(index[0].close) > 0:
        index_return = (float(index[-1].close) / float(index[0].close) - 1) * 100
        paper_return = (curve[-1][1] / book.capital - 1) * 100
        out["paper_excess"] = paper_return - index_return
    return out
