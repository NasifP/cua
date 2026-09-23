"""EGX robo-advisor: a safety-gated rebalancer for the Thndr simulator.

This package is stdlib-only by design. The demo-mode guard decides whether the
bot may click, so it must load and run on a bare interpreter; cua, OCR, the
dashboard and the data providers are optional extras layered on top.

Start at `safety.guarded_interface.GuardedInterface` -- the chokepoint every
input to the screen passes through.
"""

__version__ = "0.1.0"
