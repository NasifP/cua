"""The desktop app: one window holding the dashboard, the chat and Thndr X.

Split so that only `app.py` needs Qt. The bridge and its client are plain
Python, testable without a display.
"""
