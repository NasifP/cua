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
5. Speculation: what short-term trading needs and the app does not have yet.
   These stay locked ("needs development") until the feature is built.
6. Real money: only after months of paper trading that beat the EGX 30 after
   costs. Even then the operator presses Buy; nothing here changes that.

Passing every checkpoint is not a promise of profit. It is the least the bot
must show before it is worth trying.
"""

from __future__ import annotations

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

    @property
    def done(self) -> bool:
        return self.have >= self.need and (self.hit_rate is None or self.hit_rate >= HIT_RATE)


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


STAGES: tuple[tuple[str, str], ...] = (
    ("Reading the market", "قراءة السوق"),
    ("Your style", "أسلوبك في التداول"),
    ("Testing ideas", "اختبار الأفكار"),
    ("A real track record", "سجل حقيقي"),
    ("Speculation (needs development)", "المضاربة (محتاجة تطوير)"),
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
        ("Size each trade from its stop distance (ATR) so one loss is a fixed small share "
         "of the account, e.g. 1%.",
         "حجم كل صفقة يتحسب من المسافة للـ Stop (ATR)، علشان الخسارة الواحدة تبقى نسبة "
         "صغيرة ثابتة من الحساب، زي 1%.")),
    Checkpoint(
        "paper", 5,
        ("Paper trading journal", "دفتر تداول تجريبي"),
        ("Every virtual trade written down: entry, stop, target, exit, fees and result. "
         "The track record speculation is judged on.",
         "كل صفقة وهمية متسجلة: الدخول، الـ Stop، الهدف، الخروج، المصاريف والنتيجة. ده "
         "السجل اللي المضاربة بتتحاسب عليه.")),
    Checkpoint(
        "exits", 5,
        ("Exit discipline", "الالتزام بالخروج"),
        ("Every paper trade leaves at its stop or target, never held \"until it comes back\". "
         "Measured on the paper journal.",
         "كل صفقة تجريبية بتخرج عند الـ Stop أو الهدف، مش بتفضل مستنية \"لحد ما ترجع\". "
         "بيتقاس من دفتر التداول التجريبي.")),
    Checkpoint(
        "loss_limits", 5,
        ("Daily loss limit and drawdown", "حد خسارة يومي وأقصى تراجع"),
        ("A daily loss limit and a maximum drawdown that halt the bot by themselves.",
         "حد أقصى للخسارة في اليوم وحد لأقصى تراجع في الحساب، ولو اتعدوا البوت يقف "
         "لوحده.")),
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
         "الخسارة. ساعتها بس يستاهل نجرب بفلوس حقيقية، وبرضه إنت اللي بتدوس Buy.")),
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
    )
