#!/usr/bin/env python3
"""Read the live Thndr screen and print what the guard can actually see.

    python calibrate_ui.py --target host --os macos

Open Thndr in your browser, switch it to the **simulator**, then run this. It
screenshots the screen, pulls the accessibility tree, runs every demo-mode probe,
and prints the result, so the labels in `config/thndr.ui.toml` can be copied from
what the page really exposes instead of guessed.

**This script never clicks.** It only uses read-only primitives, and it runs
them through `GuardedInterface`, so the kill switch still applies. You can point
it at a screen showing real money without risk -- and doing so is a useful test,
because the guard should say CONFIRMED_LIVE.

What to do with the output
--------------------------
1. Read the candidate labels. Copy the real strings into `config/thndr.ui.toml`.
2. Check the verdict says CONFIRMED_DEMO on the simulator, and CONFIRMED_LIVE
   when you switch to the real account. If the simulator reads INDETERMINATE,
   the marker lists in `safety/demo_guard.py` need a token this page uses.
3. Only then set `calibration_complete = true`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from egx_advisor.bus import StateBus  # noqa: E402
from egx_advisor.cua_runtime import require_cua_python  # noqa: E402
from egx_advisor.execution.thndr import ThndrUiMap  # noqa: E402
from egx_advisor.safety.demo_guard import (  # noqa: E402
    DEMO_TOKENS,
    LIVE_TOKENS,
    ActionRisk,
    DemoGuard,
)
from egx_advisor.safety.guarded_interface import GuardedInterface  # noqa: E402
from egx_advisor.safety.vision import (  # noqa: E402
    _walk_labels,
    contains_token,
    ocr_screen_lines,
)


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="inspect the live UI, read-only")
    parser.add_argument("--target", choices=("host", "cloud"), default="host")
    parser.add_argument("--os", dest="os_type", choices=("macos", "linux", "windows"))
    parser.add_argument("--ui", default="config/thndr.ui.toml")
    parser.add_argument("--out", default="state/calibration",
                        help="where to save the screenshot and accessibility tree")
    parser.add_argument("--max-labels", type=int, default=120)
    args = parser.parse_args()

    require_cua_python(Path(__file__).resolve().parent)
    try:
        from computer import Computer
    except ImportError as exc:
        print(f"cua is not installed: {exc}\n  pip install -e '.[agent]'", file=sys.stderr)
        return 1

    import os
    import platform

    os_type = args.os_type or {"Darwin": "macos", "Windows": "windows"}.get(
        platform.system(), "linux"
    )
    if args.target == "host":
        computer = Computer(use_host_computer_server=True, os_type=os_type)
    else:
        computer = Computer(
            os_type=os_type, provider_type="cloud",
            name=os.environ.get("CUA_CONTAINER_NAME", ""),
            api_key=os.environ.get("CUA_API_KEY", ""),
        )

    async with computer:
        raw = computer.interface
        # A scratch bus: this tool must not disturb the agent's control state.
        bus = StateBus(Path(args.out) / "calibration_bus.db")
        bus.resume(actor="calibrate_ui", reason="read-only inspection")
        guarded = GuardedInterface(
            raw, bus=bus, guard=DemoGuard.default(),
            accessibility_tree_provider=getattr(raw, "get_accessibility_tree", None),
        )

        rule("SCREEN")
        size = await guarded.get_screen_size()
        print(f"  {size}")

        screenshot = await guarded.screenshot()
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        shot_path = out / "screen.png"
        shot_path.write_bytes(screenshot or b"")
        print(f"  screenshot -> {shot_path} ({len(screenshot or b'')} bytes)")

        tree = None
        try:
            tree = await raw.get_accessibility_tree()
        except Exception as exc:  # noqa: BLE001
            print(f"  accessibility tree unavailable: {exc}")

        labels: list[str] = []
        if tree:
            (out / "tree.json").write_text(
                json.dumps(tree, indent=2, ensure_ascii=False)[:2_000_000], encoding="utf-8"
            )
            labels = [x.strip() for x in _walk_labels(tree) if x and x.strip()]
            print(f"  accessibility tree -> {out / 'tree.json'} ({len(labels)} labels)")

        # On Windows the tree cua returns holds window titles, not page content,
        # so without OCR every label below would read MISS on every screen.
        ocr_lines, ocr_error = ocr_screen_lines(screenshot or b"")
        if ocr_error:
            print(f"  OCR unavailable: {ocr_error}")
            print("  On Windows the accessibility tree lists window titles only, so")
            print("  without OCR this tool cannot see the page. Install Tesseract with")
            print("  the Arabic language pack (see the README), then run this again.")
        else:
            (out / "ocr.txt").write_text("\n".join(ocr_lines), encoding="utf-8")
            print(f"  OCR -> {out / 'ocr.txt'} ({len(ocr_lines)} lines)")
        labels = labels + ocr_lines

        rule("WHAT THE GUARD DECIDED")
        verdict = await guarded.assert_demo_now()
        print(f"  state             {verdict.state.value.upper()}")
        print(f"  confidence        {verdict.confidence:.2f}")
        print(f"  sources           {verdict.effective_sources}")
        print(f"  probes run        {list(verdict.probes_run) or 'NONE'}")
        print(f"  probes missing    {list(verdict.probes_unavailable) or 'none'}")
        print(f"  detail            {verdict.detail}")
        for evidence in verdict.evidence:
            print(f"    - {evidence.describe()}")
        for risk in (ActionRisk.NAVIGATION, ActionRisk.ORDER_CRITICAL):
            ok, why = verdict.permits(risk)
            print(f"  permits {risk.value:15} {'yes' if ok else 'NO '}  ({why})")

        if not verdict.probes_run:
            print("\n  !! No probe could run. Install OCR (pip install -e '.[ocr]')")
            print("     or check that the accessibility tree is reachable. With no")
            print("     probe the guard can never confirm, so the bot will never click.")

        rule("MARKER MATCHES IN THE ACCESSIBILITY LABELS AND OCR TEXT")
        if labels:
            joined = " \n".join(labels)
            hit_demo = [t for t in DEMO_TOKENS if contains_token(joined, t)]
            hit_live = [t for t in LIVE_TOKENS if contains_token(joined, t)]
            print(f"  simulator markers matched : {hit_demo or 'NONE'}")
            print(f"  real-money markers matched: {hit_live or 'none'}")
            if not hit_demo and not hit_live:
                print("\n  Neither list matched. If this screen IS the simulator, add the")
                print("  token this page uses to DEMO_TOKENS in safety/demo_guard.py.")
                print("  Look for it among the candidate labels below.")
        else:
            print("  no accessibility labels or OCR text to match against")

        rule(f"CANDIDATE LABELS (first {args.max_labels})")
        print("  Copy the real strings into config/thndr.ui.toml.\n")
        seen: set[str] = set()
        shown = 0
        for label in labels:
            if label in seen or len(label) > 90:
                continue
            seen.add(label)
            print(f"    {label!r}")
            shown += 1
            if shown >= args.max_labels:
                print(f"    ... ({len(set(labels)) - shown} more in tree.json)")
                break
        if not labels:
            print("    none -- read the screenshot by eye instead")

        rule("CURRENT CONFIG vs WHAT IS ON SCREEN")
        try:
            ui = ThndrUiMap.from_toml(args.ui)
        except Exception as exc:  # noqa: BLE001
            print(f"  could not load {args.ui}: {exc}")
            ui = ThndrUiMap()
        joined = " \n".join(labels)
        for name in (
            "account_switcher_label", "simulator_option_label", "portfolio_tab_label",
            "search_label", "buy_button_label", "sell_button_label",
            "quantity_field_label", "limit_price_field_label",
            "review_button_label", "confirm_button_label", "order_placed_label",
        ):
            value = getattr(ui, name)
            found = contains_token(joined, value) if labels else None
            mark = "  ?  " if found is None else ("  ok " if found else " MISS")
            print(f"  [{mark}] {name:24} {value!r}")
        print("\n  MISS means the label is not on THIS screen. That is expected for")
        print("  controls on other screens (Buy/Confirm live in the order ticket).")
        print("  Re-run this on each screen you care about before trusting a label.")

        rule("NEXT")
        print(f"  calibration_complete is currently {ui.calibration_complete}.")
        print("  Set it to true in config/thndr.ui.toml only after every label above")
        print("  has been checked on the screen it belongs to, and the verdict reads")
        print("  CONFIRMED_DEMO on the simulator and CONFIRMED_LIVE on the real account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
