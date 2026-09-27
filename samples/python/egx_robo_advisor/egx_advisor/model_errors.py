"""Model-provider errors as one line a person can act on.

Providers answer failures with pages of JSON. The operator needs to know
which of a few things went wrong and what to change, in their language.
"""

from __future__ import annotations

from .i18n import tr


def explain(exc: BaseException, model: str, lang: str | None = None) -> str:
    text = str(exc)
    lower = text.lower()
    if "404" in text or "notfound" in lower or "no longer available" in lower \
            or "not found" in lower:
        key = "model.gone"
    elif "429" in text or "ratelimit" in lower or "quota" in lower:
        key = "model.quota"
    elif "401" in text or "403" in text or "api key" in lower or "permission" in lower \
            or "authentication" in lower:
        key = "model.key"
    elif "timeout" in lower or "timed out" in lower:
        key = "model.timeout"
    else:
        first = text.strip().splitlines()[0] if text.strip() else type(exc).__name__
        return tr("model.other", lang=lang, model=model, error=first[:160])
    return tr(key, lang=lang, model=model)
