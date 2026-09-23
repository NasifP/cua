"""Safety layer: the demo-mode guard and the only sanctioned path to the screen."""

from .demo_guard import (
    ActionRisk,
    DemoGuard,
    DemoModeViolation,
    DemoState,
    DemoVerdict,
    Evidence,
)
from .guarded_interface import GuardedInterface, KillSwitchEngaged, classify

__all__ = [
    "ActionRisk",
    "DemoGuard",
    "DemoModeViolation",
    "DemoState",
    "DemoVerdict",
    "Evidence",
    "GuardedInterface",
    "KillSwitchEngaged",
    "classify",
]
