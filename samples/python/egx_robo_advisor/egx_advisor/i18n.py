"""The desktop app's two languages: Arabic (the default) and English.

    from egx_advisor.i18n import tr
    button.setText(tr("lab.run"))

The choice is kept in .env as EGX_LANG. Every key has both languages; a test
checks that. Log lines, the command-line tools and error details from the bot
stay in English -- they are what a bug report quotes.

The dashboard page is standalone HTML, so it carries its own table of strings
(dashboard/templates/index.html), switched the same way.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

LANGS = ("ar", "en")
DEFAULT = "ar"

_current = DEFAULT

#: key -> (English, Arabic)
STRINGS: dict[str, tuple[str, str]] = {
    # --- window ---
    "app.brand_sub": ("Egyptian Exchange advisor", "مستشار البورصة المصرية"),
    "app.halt": ("HALT", "إيقاف البوت"),
    "app.halt_tip": ("Stops the bot at once (Ctrl+Shift+H)", "يوقف البوت فوراً (Ctrl+Shift+H)"),
    "app.halt_hint": ("Ctrl+Shift+H  ·  one press stops it", "Ctrl+Shift+H  ·  الإيقاف ضغطة واحدة"),
    "app.theme_light": ("Light mode", "وضع فاتح"),
    "app.theme_dark": ("Dark mode", "وضع داكن"),
    "app.switch_lang": ("العربية", "English"),
    "app.state_halted": ("HALTED", "متوقف"),
    "app.state_armed": ("ARMED", "شغّال"),
    "app.state_unknown": ("UNKNOWN", "الحالة غير معروفة"),
    "app.spend": ("Models today", "الموديلات النهارده"),
    "app.starting": ("Starting the dashboard ...", "لوحة التحكم بتفتح ..."),
    "app.restarting": ("Restarting ...", "بيعيد التشغيل ..."),
    "app.halt_failed": ("Could not halt the bot: {error}", "ماقدرتش أوقف البوت: {error}"),
    "app.fix_first": ("Fix these first (see .env), then start again:",
                      "صلّح دول الأول (في ملف .env) وبعدين شغّل تاني:"),
    "app.missing_keys": ("Before the bot can read your portfolio: {items}. Enter the key "
                         "below and press Save.",
                         "قبل ما البوت يقدر يقرا محفظتك: {items}. اكتب المفتاح تحت واضغط حفظ."),

    # --- pages: title, subtitle ---
    "page.dashboard": ("Dashboard", "لوحة التحكم"),
    "page.dashboard.sub": ("Plan, holdings, log and chat. Start the bot here.",
                           "الخطة والمحفظة والسجل والشات. من هنا بتشغّل البوت."),
    "page.thndr": ("Thndr X", "Thndr X"),
    "page.thndr.sub": ("Your broker in the app's own browser. The bot only reads it; the "
                       "panel fills a buy ticket when you press Fill, and you press Buy.",
                       "حسابك في متصفح البرنامج. البوت بيقراه بس؛ واللوحة بتملى أمر الشراء "
                       "لما تضغط املأ، وانت اللي بتضغط شراء."),
    "page.chart": ("Chart", "الشارت"),
    "page.chart.sub": ("TradingView chart for your holdings. Nothing here reaches the bot.",
                       "شارت TradingView لأسهمك. اللي هنا ليك انت، ومش بيوصل للبوت."),
    "page.lab": ("Strategy Lab", "معمل الاستراتيجيات"),
    "page.lab.sub": ("Test an indicator rule on real EGX history before the bot uses it.",
                     "جرّب قاعدة مؤشر على تاريخ البورصة الحقيقي قبل ما البوت يستخدمها."),
    "page.sources": ("Events & news", "الأحداث والأخبار"),
    "page.sources.sub": ("Dates and news sources that pause new buys.",
                         "مواعيد ومصادر أخبار بتوقّف الشراء الجديد وقت الخطر."),
    "page.settings": ("Settings", "الإعدادات"),
    "page.settings.sub": ("API keys, models and limits.", "مفاتيح API والموديلات والحدود."),

    # --- chart ---
    "chart.symbol": ("Symbol", "السهم"),
    "chart.placeholder": ("COMI.CA or TVC:GOLD", "COMI.CA أو TVC:GOLD"),
    "chart.symbol_tip": ("Pick a holding, or type any TradingView symbol and press Enter",
                         "اختار سهم من محفظتك، أو اكتب أي رمز من TradingView واضغط Enter"),
    "chart.refresh": ("Refresh list", "حدّث القائمة"),
    "chart.refresh_tip": ("Reload your holdings and the policy universe",
                          "هات أسهم محفظتك وأسهم الخطة من جديد"),
    "chart.failed": ("The TradingView chart could not load. Check the internet connection "
                     "and try again.",
                     "تعذّر تحميل شارت TradingView. اتأكد من الإنترنت وجرّب تاني."),

    # --- strategy lab ---
    "lab.intro": ("A rule can only skip buys, and is adopted only if it wins after costs "
                  "over the whole history and in each half.",
                  "القاعدة تقدر بس تمنع شراء، ومش هتتعتمد إلا لو كسبت بعد العمولات في "
                  "التاريخ كله وفي كل نص منه لوحده."),
    "lab.rule_box": ("Rule", "القاعدة"),
    "lab.rule": ("Rule", "القاعدة"),
    "lab.history": ("History", "التاريخ"),
    "lab.years": (" years", " سنين"),
    "lab.prices": ("Prices", "الأسعار"),
    "lab.real": ("Real EGX prices (Yahoo)", "أسعار البورصة الحقيقية (Yahoo)"),
    "lab.synthetic": ("Synthetic: checks the program only, never adoptable",
                      "أسعار وهمية: لاختبار البرنامج بس، ومش بتتعتمد"),
    "lab.run": ("Run the test", "شغّل الاختبار"),
    "lab.run_tip": ("Replay the policy with and without the rule",
                    "يعيد تشغيل الخطة بالقاعدة ومن غيرها"),
    "lab.result_box": ("Result", "النتيجة"),
    "lab.adopt": ("Adopt the rule", "اعتمد القاعدة"),
    "lab.adopt_tip": ("Enabled only for a rule that passed", "بيشتغل بس للقاعدة اللي نجحت"),
    "lab.adopted_box": ("Rules the bot uses", "القواعد اللي البوت بيستخدمها"),
    "lab.remove": ("Remove selected", "احذف المحددة"),
    "lab.none": ("No adopted rules yet", "لسه مفيش قواعد معتمدة"),
    "lab.unreadable": ("rules file unreadable: {error}", "ملف القواعد مش مقروء: {error}"),
    "lab.adopted_item": ("{rule}   (adopted {day}; {evidence})",
                         "{rule}   (اتعتمدت {day}؛ {evidence})"),
    "lab.hint": ("Pick a rule and press \"Run the test\". The result appears here: return, "
                 "largest fall, costs, and the verdict.",
                 "اختار قاعدة واضغط \"شغّل الاختبار\". النتيجة هتظهر هنا: العائد، وأقصى "
                 "نزول، والعمولات، والحكم النهائي."),
    "lab.fetching": ("Fetching {years} years of prices and replaying ...",
                     "بيجيب أسعار {years} سنين ويعيد التشغيل ..."),
    "lab.running": ("Testing ... the first run can take a minute.",
                    "بيجرّب ... ممكن ياخد دقيقة أول مرة."),
    "lab.could_not_run": ("The test could not run. Details below.",
                          "الاختبار ماشتغلش. التفاصيل تحت."),
    "lab.passed": ("Passed: it won after costs over the whole history and in each half. "
                   "You can adopt it.",
                   "نجحت: كسبت بعد العمولات في التاريخ كله وفي كل نص. تقدر تعتمدها."),
    "lab.not_adopted": ("Not adoptable: {verdict}", "مش هتتعتمد: {verdict}"),
    "lab.adopted": ("Adopted. The bot applies it from its next cycle.",
                    "اتعتمدت. البوت هيطبّقها من الدورة الجاية."),
    "lab.kind.confluence": ("Trend + Momentum + Volume + Volatility, all together",
                            "الاتجاه + الزخم + الحجم + التذبذب، مع بعض"),
    "lab.kind.sma": ("Skip buys when the price is below its N-day average",
                     "منع الشراء لو السعر تحت متوسط N يوم"),
    "lab.kind.rsi": ("Skip buys when RSI is above a level (overbought)",
                     "منع الشراء لو RSI فوق مستوى معيّن (تشبّع شراء)"),
    "lab.kind.momentum": ("Skip buys after a sharp fall over N days",
                          "منع الشراء بعد نزول حاد في N يوم"),
    "lab.param.trend_days": ("Trend: average of N days", "الاتجاه: متوسط N يوم"),
    "lab.param.momentum_days": ("Momentum: over N days", "الزخم: على مدى N يوم"),
    "lab.param.volume_days": ("Volume: compared with N days", "الحجم: مقارنة بـ N يوم"),
    "lab.param.volume_ratio": ("Volume: at least % of average", "الحجم: على الأقل % من المتوسط"),
    "lab.param.vol_days": ("Volatility: over N days", "التذبذب: على مدى N يوم"),
    "lab.param.max_vol_pct": ("Volatility: at most % a day", "التذبذب: بحد أقصى % في اليوم"),
    "lab.param.days": ("Days", "عدد الأيام"),
    "lab.param.above": ("RSI level", "مستوى RSI"),
    "lab.param.fall_pct": ("Fall %", "نسبة النزول %"),

    # --- ticket filling (desktop/ticket_panel.py) ---
    "ticket.title": ("Fill a buy ticket", "تجهيز أمر شراء"),
    "ticket.intro": ("The app writes the quantity and price into the open Thndr ticket. It "
                     "never presses Buy: you check the numbers and press it yourself.",
                     "البرنامج بيكتب الكمية والسعر في أمر الشراء المفتوح في Thndr. عمره ما "
                     "بيضغط شراء: انت اللي بتراجع الأرقام وتضغط بنفسك."),
    "ticket.steps": ("1. In Thndr, open the stock's buy ticket and choose a limit order.\n"
                     "2. Pick the order below and press Fill.\n"
                     "3. Check the numbers in Thndr, then press Buy there yourself.",
                     "1. في Thndr افتح أمر شراء السهم واختار أمر بسعر محدد (Limit).\n"
                     "2. اختار الأمر من القائمة تحت واضغط املأ.\n"
                     "3. راجع الأرقام في Thndr، وبعدين اضغط شراء هناك بنفسك."),
    "ticket.teach_box": ("Ticket boxes", "خانات الأمر"),
    "ticket.field.quantity": ("quantity box", "خانة الكمية"),
    "ticket.field.price": ("price box", "خانة السعر"),
    "ticket.taught": ("taught", "متعلّمة"),
    "ticket.not_taught": ("not taught", "مش متعلّمة"),
    "ticket.teach": ("Teach", "علّمها"),
    "ticket.cancel": ("Cancel teaching", "إلغاء التعليم"),
    "ticket.picking": ("Click the {field} in the open Thndr ticket. The click only selects it; "
                       "Thndr does not receive it.",
                       "اضغط على {field} في أمر Thndr المفتوح. الضغطة بتحددها بس، وThndr "
                       "مش بيستلمها."),
    "ticket.picked": ("The {field} is taught.", "{field} اتعلّمت."),
    "ticket.not_a_box": ("That is not a number box. Click inside the box itself.",
                         "دي مش خانة أرقام. اضغط جوّه الخانة نفسها."),
    "ticket.orders_box": ("Buy orders in the plan", "أوامر الشراء في الخطة"),
    "ticket.no_orders": ("No buy orders in the current plan.", "مفيش أوامر شراء في الخطة الحالية."),
    "ticket.order_line": ("{symbol}  ·  {quantity} shares  ·  limit {price}",
                          "{symbol}  ·  {quantity} سهم  ·  بسعر {price}"),
    "ticket.fill": ("Fill the ticket", "املأ الأمر"),
    "ticket.filled": ("Written: {quantity} shares of {symbol} at {price}. Check the ticket in "
                      "Thndr and press Buy yourself.",
                      "اتكتب: {quantity} سهم من {symbol} بسعر {price}. راجع الأمر في Thndr "
                      "واضغط شراء بنفسك."),
    "ticket.off": ("Ticket filling is off. Turn it on in Settings.",
                   "تجهيز الأوامر مقفول. شغّله من الإعدادات."),
    "ticket.halted": ("The bot is halted, so no ticket is filled. Start it from the Dashboard "
                      "first.",
                      "البوت متوقف، فمفيش تجهيز أوامر. شغّله من لوحة التحكم الأول."),
    "ticket.buys_only": ("Only buy tickets are filled for now.",
                         "حالياً بيجهّز أوامر الشراء بس."),
    "ticket.bad_quantity": ("The quantity {quantity} is not a whole number of shares.",
                            "الكمية {quantity} مش عدد أسهم صحيح."),
    "ticket.bad_price": ("The limit price {price} is not valid.", "السعر {price} مش صحيح."),
    "ticket.no_plan": ("There is no plan yet.", "لسه مفيش خطة."),
    "ticket.stale": ("The plan is {minutes} minutes old, and prices older than {limit} minutes "
                     "are not used. Wait for the next cycle.",
                     "الخطة عمرها {minutes} دقيقة، والأسعار الأقدم من {limit} دقيقة مش "
                     "بتتستخدم. استنى الدورة الجاية."),
    "ticket.plan_changed": ("A new plan arrived. Check the order again and press Fill.",
                            "وصلت خطة جديدة. راجع الأمر تاني واضغط املأ."),
    "ticket.teach_first": ("Teach the quantity and price boxes first.",
                           "علّم خانة الكمية وخانة السعر الأول."),
    "ticket.symbol_not_on_page": ("{ticker} is not on the Thndr page. Open that stock's buy "
                                  "ticket first.",
                                  "{ticker} مش ظاهر في صفحة Thndr. افتح أمر شراء السهم ده الأول."),
    "ticket.field_missing": ("The {field} is not on the page. Open the buy ticket, or teach "
                             "the box again.",
                             "{field} مش موجودة في الصفحة. افتح أمر الشراء، أو علّم الخانة "
                             "تاني."),
    "ticket.not_a_text_box": ("The taught {field} is not a number box any more. Teach it again.",
                              "{field} اللي اتعلّمت مابقتش خانة أرقام. علّمها تاني."),
    "ticket.field_locked": ("The {field} is locked on the page.", "{field} مقفولة في الصفحة."),
    "ticket.same_box": ("Quantity and price point to the same box. Teach them again.",
                        "الكمية والسعر بيشاوروا على نفس الخانة. علّمهم تاني."),
    "ticket.page_error": ("The page did not answer. Try again.", "الصفحة ماردّتش. جرّب تاني."),
    "ticket.readback_failed": ("Could not read the boxes back ({reason}). Check the ticket by "
                               "eye before pressing Buy.",
                               "ماقدرتش أقرا الخانات تاني ({reason}). راجع الأمر بعينك قبل "
                               "ما تضغط شراء."),
    "ticket.mismatch": ("The boxes show quantity {quantity} and price {price}, which is not the "
                        "order. Correct them or press Fill again, and do not press Buy until "
                        "they match.",
                        "الخانات فيها كمية {quantity} وسعر {price}، ودول مش الأمر. صلّحهم أو "
                        "اضغط املأ تاني، وماتضغطش شراء غير لما يطابقوا."),
    "field.EGX_FOUR_FACTOR": ("Four-factor strategy", "استراتيجية الأربع عوامل"),
    "field.EGX_FOUR_FACTOR.help": ("Trend, momentum, volume and volatility decide what to buy, "
                                   "sell and how much, instead of the fixed allocation. Compare "
                                   "them in the Strategy Lab first.",
                                   "الاتجاه والزخم والحجم والتذبذب بيحددوا تشتري إيه وتبيع "
                                   "إيه وبكام، بدل التوزيع الثابت. قارنهم في معمل "
                                   "الاستراتيجيات الأول."),
    "field.EGX_TICKET_FILL": ("Fill buy tickets in Thndr X", "تجهيز أوامر الشراء في Thndr X"),
    "field.EGX_TICKET_FILL.help": ("When on, the Thndr X page can write an order's quantity and "
                                   "price into an open buy ticket when you press Fill. It never "
                                   "presses Buy.",
                                   "لما يكون شغّال، صفحة Thndr X تقدر تكتب الكمية والسعر في أمر "
                                   "شراء مفتوح لما تضغط املأ. عمرها ما بتضغط شراء."),

    # --- rule descriptions (strategy/filters.py) ---
    "rule.confluence": ("buy only when all agree: close above its {trend_days}-day average, "
                        "rising over {momentum_days} days, 5-day volume at least "
                        "{volume_ratio}% of {volume_days}-day, daily volatility at most "
                        "{max_vol_pct}% over {vol_days} days",
                        "شراء بس لما الأربعة يتفقوا: السعر فوق متوسط {trend_days} يوم، وطالع "
                        "في آخر {momentum_days} يوم، وحجم آخر 5 أيام على الأقل {volume_ratio}% "
                        "من متوسط {volume_days} يوم، والتذبذب اليومي بحد أقصى {max_vol_pct}% "
                        "في آخر {vol_days} يوم"),
    "rule.sma": ("no buys below the {days}-day average", "مفيش شراء تحت متوسط {days} يوم"),
    "rule.rsi": ("no buys when RSI({days}) > {above}", "مفيش شراء لو RSI({days}) أكبر من {above}"),
    "rule.momentum": ("no buys after a {fall}% fall over {days} days",
                      "مفيش شراء بعد نزول {fall}% في {days} يوم"),

    # --- lab report (backtest/lab.py) ---
    "report.four_factor": ("four-factor", "الأربع عوامل"),
    "report.strategy": ("Strategy", "الاستراتيجية"),
    "report.versus_strategy": ("Four-factor strategy vs plain policy (annualised):",
                               "استراتيجية الأربع عوامل مقابل الخطة الحالية (سنوياً):"),
    "lab.kind.four_factor_strategy": ("Trend + Momentum + Volume + Volatility decide what to "
                                      "hold and how much",
                                      "الاتجاه + الزخم + الحجم + التذبذب بيحددوا نمسك إيه "
                                      "وبكام"),
    "lab.compare": ("Compare the full strategy with the current plan",
                    "قارن الاستراتيجية الكاملة بالخطة الحالية"),
    "lab.compare_tip": ("Four factors decide buys, sells and sizes; replayed against the "
                        "fixed allocation",
                        "الأربع عوامل بيقرروا الشراء والبيع والحجم، ويتقارنوا بالتوزيع الثابت"),
    "lab.strategy_passed": ("The four-factor strategy beat the current plan after costs, over "
                            "the whole history and in each half. You can switch to it in "
                            "Settings.",
                            "استراتيجية الأربع عوامل كسبت الخطة الحالية بعد العمولات، في "
                            "التاريخ كله وفي كل نص. تقدر تشغّلها من الإعدادات."),
    "lab.strategy_failed": ("Not recommended yet: {verdict}",
                            "مش موصى بيها لسه: {verdict}"),
    "report.rules": ("Rules", "القواعد"),
    "report.with_rules": ("with rules", "بالقواعد"),
    "report.policy": ("policy", "الخطة"),
    "report.hold": ("buy & hold", "شراء واحتفاظ"),
    "report.total_return": ("total return", "العائد الكلي"),
    "report.max_drawdown": ("max drawdown", "أقصى نزول"),
    "report.costs": ("costs paid", "العمولات"),
    "report.fills": ("fills", "الصفقات"),
    "report.versus": ("With rules vs plain policy (annualised):",
                      "بالقواعد مقابل الخطة من غيرها (سنوياً):"),
    "report.whole": ("whole history", "التاريخ كله"),
    "report.interval": ("95% interval", "مدى الثقة 95%"),
    "report.first": ("first half", "النص الأول"),
    "report.second": ("second half", "النص التاني"),
    "report.verdict": ("Verdict", "الحكم"),
    "report.note": ("Note", "ملحوظة"),
    "verdict.synthetic": ("not adoptable: tested on synthetic prices, which say nothing "
                          "about EGX",
                          "مش هتتعتمد: اتجرّبت على أسعار وهمية، ودي مابتقولش حاجة عن البورصة"),
    "verdict.passed": ("passed: beats the plain policy after costs, in both halves",
                       "نجحت: كسبت الخطة من غيرها بعد العمولات، في النصين"),
    "verdict.noise": ("no evidence it helps: the difference is indistinguishable from noise",
                      "مفيش دليل إنها بتفيد: الفرق مش باين من العشوائية"),
    "verdict.hurts": ("it hurts: the plain policy did better after costs",
                      "بتضر: الخطة من غيرها كانت أحسن بعد العمولات"),
    "verdict.inconsistent": ("not consistent: it helped in one half of the history and not "
                             "the other",
                             "مش ثابتة: فادت في نص من التاريخ ومافادتش في التاني"),

    # --- events & news ---
    "src.calendar": ("Economic calendar", "التقويم الاقتصادي"),
    "src.calendar_note": ("No new buys around these. high: the day before, of and after. "
                          "medium: the day itself. Copy dates from the central bank's site "
                          "or Investing.com.",
                          "مفيش شراء جديد حوالين الأحداث دي. high: اليوم اللي قبل الحدث "
                          "ويومه واليوم اللي بعده. medium: يوم الحدث بس. انقل المواعيد من "
                          "موقع البنك المركزي أو Investing.com."),
    "src.col_date": ("Date", "التاريخ"),
    "src.col_event": ("Event", "الحدث"),
    "src.col_impact": ("Impact", "الأهمية"),
    "src.event_placeholder": ("e.g. central bank rate decision",
                              "مثلاً: قرار الفايدة من البنك المركزي"),
    "src.add_event": ("Add event", "أضف الحدث"),
    "src.remove": ("Remove selected", "احذف المحدد"),
    "src.no_events": ("No events yet. Add the next central bank meeting from cbe.org.eg.",
                      "لسه مفيش أحداث. ضيف ميعاد اجتماع البنك المركزي الجاي من cbe.org.eg."),
    "src.news": ("News sources", "مصادر الأخبار"),
    "src.news_note": ("RSS links only, no logins. Check a site's terms before adding it. If "
                      "an official source fails during a session, buys stop. Changes apply "
                      "after the bot restarts.",
                      "روابط RSS بس، ومن غير تسجيل دخول. اتأكد إن شروط الموقع بتسمح قبل ما "
                      "تضيفه. لو مصدر رسمي وقع وقت التداول، الشراء بيقف. التعديل بيشتغل بعد "
                      "إعادة تشغيل البوت."),
    "src.col_name": ("Name", "الاسم"),
    "src.col_url": ("Link", "الرابط"),
    "src.col_official": ("Official", "رسمي"),
    "src.col_status": ("Status", "الحالة"),
    "src.name": ("Name", "الاسم"),
    "src.url": ("https://...  RSS or Atom link", "https://...  رابط RSS أو Atom"),
    "src.official": ("Official source (exchange or regulator)",
                     "مصدر رسمي (البورصة أو الرقابة)"),
    "src.official_yes": ("official", "رسمي"),
    "src.on": ("on", "شغّال"),
    "src.off": ("off", "متوقف"),
    "src.add_feed": ("Add source", "أضف المصدر"),
    "src.toggle": ("Enable / disable", "شغّل / وقّف"),
    "src.need_title": ("Give the event a title.", "اكتب اسم الحدث."),
    "src.added_event": ("Added {title} on {day}. Applies from the bot's next cycle.",
                        "اتضاف {title} يوم {day}. هيتطبّق من دورة البوت الجاية."),
    "src.removed_event": ("Removed {title}.", "اتشال {title}."),
    "src.need_url": ("A feed needs an http(s) link.", "المصدر محتاج رابط http أو https."),
    "src.added_feed": ("Source added. The bot reads it after its next restart.",
                       "المصدر اتضاف. البوت هيقراه بعد ما يعيد التشغيل."),
    "src.saved": ("Saved. Applies after the bot's next restart.",
                  "اتحفظ. هيتطبّق بعد ما البوت يعيد التشغيل."),
    "src.removed_feed": ("Removed. Applies after the bot's next restart.",
                         "اتشال. هيتطبّق بعد ما البوت يعيد التشغيل."),
    "src.events_unreadable": ("events.toml unreadable: {error}",
                              "ملف events.toml مش مقروء: {error}"),
    "src.feeds_unreadable": ("feeds.toml unreadable: {error}",
                             "ملف feeds.toml مش مقروء: {error}"),

    # --- settings ---
    "set.keys": ("API keys", "مفاتيح API"),
    "set.keys_note": ("Keys are saved in {where} and never shown again. To change one, type "
                      "the new key over it.",
                      "المفاتيح بتتحفظ في {where}، ومش بتظهر تاني بعد الحفظ. لو عاوز تغيّر "
                      "مفتاح، اكتب الجديد مكانه."),
    "set.where_env": (".env (no credential store on this system)",
                      "ملف .env (مفيش مخزن مفاتيح على الجهاز ده)"),
    "set.where_windows": ("Windows Credential Manager", "Windows Credential Manager"),
    "set.where_keyring": ("the system keyring", "مخزن مفاتيح النظام"),
    "set.saved_pill": ("saved", "محفوظ"),
    "set.not_set": ("not set", "مش متسجل"),
    "set.will_remove": ("removed on Save", "هيتمسح لما تحفظ"),
    "set.remove": ("Remove", "حذف"),
    "set.remove_tip": ("Remove the stored {key}", "امسح {key} المحفوظ"),
    "set.models": ("Models", "الموديلات"),
    "set.models_note": ("Any litellm id: provider/model, e.g. gemini/..., anthropic/..., "
                        "openai/..., xai/..., deepseek/..., openrouter/... . Model names "
                        "change often; check your provider's current list.",
                        "أي اسم موديل من litellm بالشكل provider/model، زي gemini/... أو "
                        "anthropic/... أو openai/... أو xai/... أو deepseek/... أو "
                        "openrouter/... . أسماء الموديلات بتتغير كتير، فراجع القائمة عند "
                        "الشركة."),
    "set.same_as_chat": ("empty: same as the Chat model", "فاضي: نفس موديل الشات"),
    "set.test": ("Test", "جرّب"),
    "set.test_tip": ("Send one short request with the saved key",
                     "يبعت طلب قصير واحد بالمفتاح المحفوظ"),
    "set.testing": ("Testing {model} ...", "بيجرّب {model} ..."),
    "set.behaviour": ("Behaviour and limits", "السلوك والحدود"),
    "set.fixed": ("Not changed here", "حاجات مش بتتغير من هنا"),
    "set.fixed.mode": ("Mode", "الوضع"),
    "set.fixed.mode_text": ("live_read_only -- the bot reads your account and never clicks or "
                            "types. Ticket filling (above) writes a buy's quantity and price "
                            "only when you press Fill in the Thndr X tab; you press Buy.",
                            "live_read_only -- البوت بيقرا حسابك بس، عمره ما بيضغط أو يكتب. "
                            "تجهيز الأوامر (فوق) بيكتب كمية وسعر أمر الشراء بس لما تضغط املأ "
                            "في صفحة Thndr X، وانت اللي بتضغط شراء."),
    "set.fixed.calibration": ("Calibration", "المعايرة"),
    "set.fixed.calibration_text": ("{state}. Changed only after measuring the real screen.",
                                   "{state}. بتتغير بس بعد قياس الشاشة الحقيقية."),
    "set.fixed.complete": ("complete", "كاملة"),
    "set.fixed.incomplete": ("not complete", "مش كاملة"),
    "set.fixed.fence": ("Submit-button fence", "حماية زرار التنفيذ"),
    "set.fixed.fence_text": ("{state}. A coordinate on a real account is not a text box "
                             "setting.",
                             "{state}. مكان زرار على حساب حقيقي مش حاجة تتكتب في خانة."),
    "set.fixed.measured": ("measured", "متقاسة"),
    "set.fixed.unmeasured": ("not measured", "مش متقاسة"),
    "set.fixed.unreadable": ("config unreadable: {error}", "الإعدادات مش مقروءة: {error}"),
    "set.fixed.targets": ("Strategy targets", "أهداف الاستراتيجية"),
    "set.fixed.targets_text": ("config/policy.egx.toml, under the no-overfit charter.",
                               "في config/policy.egx.toml، وماشية بقواعد منع الـ overfitting."),
    "set.save": ("Save settings", "حفظ الإعدادات"),
    "set.not_saved": ("Not saved: {error}", "ماتحفظش: {error}"),
    "set.saved": ("Saved.", "اتحفظ."),
    "set.keys_in_env": (" Keys went to .env because no credential store is available.",
                        " المفاتيح اتحفظت في .env لأن مفيش مخزن مفاتيح."),
    "set.restart_q": ("Saved. Restart the bot so the new settings take effect?\n\nThe bot is "
                      "halted first and has to be started again from the Dashboard.",
                      "اتحفظ. أعيد تشغيل البوت عشان الإعدادات الجديدة تشتغل؟\n\nالبوت هيقف "
                      "الأول، ولازم تشغّله تاني من لوحة التحكم."),
    "set.restarted": (" The bot restarted halted.", " البوت اشتغل من جديد وهو متوقف."),
    "set.next_start": (" Takes effect the next time the app starts.",
                       " هيتطبّق أول ما البرنامج يفتح المرة الجاية."),
    "field.GOOGLE_API_KEY.help": ("Only for a Gemini computer-use agent. The same key as "
                                  "Gemini works.",
                                  "بس لوكيل Gemini اللي بيتحكم في الشاشة. نفس مفتاح Gemini "
                                  "ينفع."),
    "field.OPENROUTER_API_KEY": ("OpenRouter (many providers)", "OpenRouter (شركات كتير)"),
    "field.EGX_VISION_MODEL": ("Reads your portfolio", "قراءة المحفظة"),
    "field.EGX_CHAT_MODEL": ("Chat", "الشات"),
    "field.EGX_CLASSIFIER_MODEL": ("News classifier", "تصنيف الأخبار"),
    "field.EGX_CHAT_ENABLED": ("Chat enabled", "تشغيل الشات"),
    "field.EGX_CHAT_ENABLED.help": ("Chat sends your holdings and their values to the chat "
                                    "model's provider.",
                                    "الشات بيبعت أسهمك وقيمتها لشركة موديل الشات."),
    "field.EGX_CYCLE_SECONDS": ("Seconds between cycles (market open)",
                                "ثواني بين كل دورة (والسوق فاتح)"),
    "field.EGX_CYCLE_SECONDS.help": ("How often the bot reads the portfolio and replans.",
                                     "كل قد إيه البوت يقرا المحفظة ويعيد الخطة."),
    "field.EGX_DAILY_SPEND_LIMIT_USD": ("Daily model spend limit (USD)",
                                        "حد صرف الموديلات في اليوم (دولار)"),
    "field.EGX_DAILY_SPEND_LIMIT_USD.help": ("Model calls stop for the day once this is "
                                             "reached.",
                                             "طلبات الموديلات بتقف باقي اليوم لما توصل للحد ده."),
    "field.EGX_DAILY_CALL_LIMIT": ("Daily model call limit", "حد طلبات الموديلات في اليوم"),
    "field.EGX_DAILY_CALL_LIMIT.help": ("A backstop that works even for models without a "
                                        "known price.",
                                        "حد احتياطي بيشتغل حتى مع الموديلات اللي سعرها مش "
                                        "معروف."),
}


def name_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    value = ((env if env is not None else os.environ).get("EGX_LANG") or "").strip().lower()
    return value if value in LANGS else DEFAULT


def current() -> str:
    return _current


def set_language(lang: str) -> str:
    global _current
    _current = lang if lang in LANGS else DEFAULT
    return _current


def is_rtl(lang: Optional[str] = None) -> bool:
    return (lang or _current) == "ar"


def tr(key: str, default: Optional[str] = None, /, lang: Optional[str] = None,
       **values: object) -> str:
    """The text for `key` in the current language, formatted with `values`.

    A key missing from the table returns `default` (or the key itself), so a
    new English-only label shows up readable rather than crashing the window.
    """
    pair = STRINGS.get(key)
    if pair is None:
        text = default if default is not None else key
    else:
        text = pair[1] if (lang or _current) == "ar" else pair[0]
    return text.format(**values) if values else text
