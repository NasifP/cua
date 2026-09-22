"""EGX robo-advisor: a safety-gated rebalancer for the Thndr simulator.

The package is stdlib-only by design. The demo-mode guard is the component that
decides whether the bot may click, so it must be able to load and run on a bare
interpreter -- cua, OCR, and the dashboard are optional extras layered on top.

Start at `egx_cua_agent.EgxCuaAgent` for the loop and its five gates, and at
`safety.guarded_interface.GuardedInterface` for the chokepoint every input
passes through.
"""

__version__ = "0.1.0"
