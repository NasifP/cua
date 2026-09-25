"""Vision probes feeding the demo-mode guard.

Three independent signal families, because one is not enough to bet an account
on. They degrade gracefully when a dependency is missing -- but degradation only
ever *removes* a source of confirmation, it never loosens the gate. A guard with
no working probe returns no evidence, and no evidence means the bot does not
click.

  TextProbe   -- reads glyphs. Tesseract when installed (eng+ara), otherwise an
                 LLM vision call, otherwise nothing.
  ColourProbe -- deterministic, dependency-free: is the badge's accent colour
                 present where the badge should be?
  AccessibilityProbe -- when the platform exposes a tree, the node labels are
                 far more reliable than pixels; used as a first-class source.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from ..paths import PROJECT_ROOT
from .pngutil import RgbImage, decode_png

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Region:
    """A fractional region of the screen, so it survives a resolution change.

    Coordinates are 0..1 of width/height. Hard-coding pixels is how a guard
    silently starts inspecting the wrong corner after someone plugs in a
    different monitor.
    """

    left: float = 0.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0
    name: str = "full"

    def __post_init__(self) -> None:
        if not (0.0 <= self.left < self.right <= 1.0):
            raise ValueError(f"bad horizontal bounds for region {self.name!r}")
        if not (0.0 <= self.top < self.bottom <= 1.0):
            raise ValueError(f"bad vertical bounds for region {self.name!r}")

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            int(self.left * width),
            int(self.top * height),
            max(int(self.left * width) + 1, int(self.right * width)),
            max(int(self.top * height) + 1, int(self.bottom * height)),
        )


#: Where broker apps put an account-mode chip: the top strip, and the header of
#: the order ticket. Both are checked; agreement raises confidence.
HEADER_REGION = Region(0.0, 0.0, 1.0, 0.18, name="header")
TICKET_REGION = Region(0.0, 0.0, 1.0, 0.45, name="ticket")
FULL_REGION = Region(name="full")


# --------------------------------------------------------------------------- #
# Text normalisation (Arabic-aware)
# --------------------------------------------------------------------------- #

_ARABIC_DIACRITICS = re.compile(r"[ً-ْـ]")
_ALEF_VARIANTS = re.compile(r"[آأإٱ]")


def normalise(text: str) -> str:
    """Fold text so Arabic and English tokens match reliably.

    Thndr renders its account-mode chip in Arabic or English depending on app
    language, and OCR output varies in alef form, ta-marbuta, and diacritics.
    Without folding, a perfectly legible Arabic badge silently fails to match.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = _ARABIC_DIACRITICS.sub("", folded)
    folded = _ALEF_VARIANTS.sub("ا", folded)  # -> bare alef
    folded = folded.replace("ى", "ي")  # alef maksura -> ya
    folded = folded.replace("ة", "ه")  # ta marbuta -> ha
    return re.sub(r"\s+", " ", folded).strip()


def contains_token(haystack: str, token: str) -> bool:
    """Whole-token containment on normalised text.

    Word boundaries matter: a bare substring search for "demo" also fires on
    "demography", and Arabic script has no case to disambiguate. For scripts
    without Latin word boundaries we fall back to substring, which is correct
    for Arabic where prefixes attach directly to the stem.
    """
    hay = normalise(haystack)
    needle = normalise(token)
    if not needle:
        return False
    if re.fullmatch(r"[\x00-\x7f ]+", needle):
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", hay) is not None
    return needle in hay


# --------------------------------------------------------------------------- #
# Probe results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TextHit:
    text: str
    confidence: float
    region: str = ""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What one probe managed to observe. `available` False means "did not run"."""

    source: str
    available: bool
    hits: tuple[TextHit, ...] = ()
    detail: str = ""

    @property
    def joined_text(self) -> str:
        return " \n".join(h.text for h in self.hits)


class TextProbe(Protocol):
    """Anything that can turn a region of a screenshot into text."""

    name: str

    def read(self, screenshot: bytes, region: Region) -> ProbeResult: ...


# --------------------------------------------------------------------------- #
# Tesseract
# --------------------------------------------------------------------------- #


#: Where Windows installers put Tesseract. It is rarely on PATH afterwards, and
#: asking an operator to edit PATH was one of the steps most likely to go wrong.
_WINDOWS_TESSERACT = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
)

#: Language packs setup.ps1 downloads into the project, so the Arabic pack needs
#: no administrator rights to install.
PROJECT_TESSDATA = PROJECT_ROOT / "state" / "tessdata"


def configure_tesseract() -> Optional[str]:
    """Point pytesseract at a Tesseract binary and language data. Idempotent.

    Returns the binary in use, or None when none can be found. Order: the
    EGX_TESSERACT_CMD override, then PATH, then the Windows install locations.
    Language data comes from state/tessdata when it holds both packs and
    TESSDATA_PREFIX is not already set.
    """
    try:
        import pytesseract
    except ImportError:
        return None
    cmd = os.environ.get("EGX_TESSERACT_CMD") or shutil.which("tesseract")
    if not cmd:
        cmd = next((c for c in _WINDOWS_TESSERACT if c and os.path.isfile(c)), None)
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    if (
        "TESSDATA_PREFIX" not in os.environ
        and (PROJECT_TESSDATA / "ara.traineddata").is_file()
        and (PROJECT_TESSDATA / "eng.traineddata").is_file()
    ):
        os.environ["TESSDATA_PREFIX"] = str(PROJECT_TESSDATA)
    return cmd


class TesseractTextProbe:
    """Local OCR. Preferred: no network, no per-call cost, fast enough to re-run
    immediately before every order-critical click."""

    name = "tesseract"

    def __init__(self, languages: str = "eng+ara", min_confidence: float = 0.55) -> None:
        self.languages = languages
        self.min_confidence = min_confidence
        self._ready: Optional[bool] = None

    def _probe_ready(self) -> bool:
        if self._ready is None:
            try:
                import pytesseract  # noqa: F401
                from PIL import Image  # noqa: F401

                configure_tesseract()
                self._ready = True
            except Exception as exc:  # noqa: BLE001
                logger.info("tesseract probe unavailable: %s", exc)
                self._ready = False
        return self._ready

    def read(self, screenshot: bytes, region: Region) -> ProbeResult:
        if not self._probe_ready():
            return ProbeResult(self.name, False, detail="pytesseract/Pillow not installed")
        try:
            import io

            import pytesseract
            from PIL import Image

            image = Image.open(io.BytesIO(screenshot)).convert("RGB")
            left, top, right, bottom = region.pixels(image.width, image.height)
            crop = image.crop((left, top, right, bottom))
            # Upscale: broker mode-chips are small, and Tesseract is much more
            # reliable above roughly 30px cap height.
            crop = crop.resize((crop.width * 2, crop.height * 2))
            data = pytesseract.image_to_data(
                crop, lang=self.languages, output_type=pytesseract.Output.DICT
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(self.name, False, detail=f"ocr failed: {exc}")

        hits: list[TextHit] = []
        # Lengths come from a third-party OCR dict. Pair what we can rather than
        # raising: fewer confirmed words is a safe outcome here, a crash is not.
        for word, raw_conf in zip(
            data.get("text", []), data.get("conf", []), strict=False
        ):
            if not word or not word.strip():
                continue
            try:
                confidence = float(raw_conf) / 100.0
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence >= self.min_confidence:
                hits.append(TextHit(word.strip(), confidence, region.name))
        return ProbeResult(self.name, True, tuple(hits), detail=f"{len(hits)} words")


# --------------------------------------------------------------------------- #
# Accessibility tree
# --------------------------------------------------------------------------- #


def ocr_screen_lines(
    screenshot: bytes, languages: str = "eng+ara", min_confidence: float = 55.0
) -> tuple[list[str], Optional[str]]:
    """Read every line of text on a screenshot, for calibration rather than gating.

    Returns ``(lines, None)``, or ``([], reason)`` when OCR cannot run. The guard
    reads fixed regions against marker lists; calibration needs the whole page,
    because on Windows the accessibility tree cua returns holds window titles
    only, so OCR is the one source that sees the page itself.
    """
    try:
        import io

        import pytesseract
        from PIL import Image
    except ImportError as exc:
        return [], f"pytesseract/Pillow not installed ({exc})"
    configure_tesseract()
    try:
        image = Image.open(io.BytesIO(screenshot)).convert("RGB")
        data = pytesseract.image_to_data(
            image, lang=languages, output_type=pytesseract.Output.DICT
        )
    except Exception as exc:  # noqa: BLE001 - a missing binary or language pack
        return [], f"OCR failed: {exc}"

    texts = data.get("text", [])
    columns = [data.get(k, []) for k in ("conf", "block_num", "par_num", "line_num")]
    lines: dict[tuple[Any, Any, Any], list[str]] = {}
    for i, raw in enumerate(texts):
        word = (raw or "").strip()
        if not word or any(i >= len(col) for col in columns):
            continue
        try:
            confidence = float(columns[0][i])
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        key = (columns[1][i], columns[2][i], columns[3][i])
        lines.setdefault(key, []).append(word)
    return [" ".join(words) for words in lines.values()], None


class AccessibilityTextProbe:
    """Reads labels from the platform accessibility tree.

    When available this is the strongest signal we have -- it is the label the
    app itself published, not an inference from pixels -- so the guard weights it
    above OCR. It is also immune to theming and font rendering.
    """

    name = "accessibility"

    def __init__(self, tree: Optional[Mapping[str, Any]] = None) -> None:
        self.tree = tree

    def read(self, screenshot: bytes, region: Region) -> ProbeResult:
        if not self.tree:
            return ProbeResult(self.name, False, detail="no accessibility tree supplied")
        labels = list(_walk_labels(self.tree))
        hits = tuple(TextHit(label, 0.99, region.name) for label in labels if label.strip())
        return ProbeResult(self.name, True, hits, detail=f"{len(hits)} nodes")


def _walk_labels(node: Any, depth: int = 0) -> Sequence[str]:
    """Flatten the label-ish string fields out of an accessibility tree."""
    if depth > 40:
        return []
    found: list[str] = []
    if isinstance(node, Mapping):
        for key in ("title", "label", "name", "value", "description", "text", "AXTitle"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value)
        for value in node.values():
            if isinstance(value, (Mapping, list, tuple)):
                found.extend(_walk_labels(value, depth + 1))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(_walk_labels(item, depth + 1))
    return found


# --------------------------------------------------------------------------- #
# LLM vision
# --------------------------------------------------------------------------- #


class LlmVisionTextProbe:
    """Last-resort text probe backed by a vision model.

    Kept last deliberately. It is slow, costs money per call, and is the least
    reproducible of the three, which is exactly the wrong profile for a check
    that runs before every click. It earns its place as a tie-breaker when OCR
    is not installed.
    """

    name = "llm_vision"

    def __init__(
        self,
        model: str = "anthropic/claude-sonnet-5",
        completion: Any = None,
        timeout: float = 20.0,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self._completion = completion

    def _resolve(self) -> Any:
        if self._completion is None:
            try:
                from litellm import completion

                self._completion = completion
            except Exception as exc:  # noqa: BLE001
                logger.info("llm vision probe unavailable: %s", exc)
                return None
        return self._completion

    def read(self, screenshot: bytes, region: Region) -> ProbeResult:
        completion = self._resolve()
        if completion is None:
            return ProbeResult(self.name, False, detail="litellm not installed")
        import base64

        encoded = base64.b64encode(screenshot).decode("ascii")
        prompt = (
            "Transcribe every badge, chip, tab, or header label in the top portion "
            "of this trading app screenshot that indicates which account is active. "
            "Output the labels verbatim, one per line, in their original language. "
            "Do not interpret, translate, or summarise. If you see none, output NONE."
        )
        try:
            response = completion(
                model=self.model,
                timeout=self.timeout,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{encoded}"},
                            },
                        ],
                    }
                ],
            )
            text = response["choices"][0]["message"]["content"] or ""
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(self.name, False, detail=f"vision call failed: {exc}")

        hits = tuple(
            TextHit(line.strip(), 0.7, region.name)
            for line in text.splitlines()
            if line.strip() and line.strip().upper() != "NONE"
        )
        return ProbeResult(self.name, True, hits, detail=f"{len(hits)} lines")


# --------------------------------------------------------------------------- #
# Colour probe
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ColourSignature:
    """An accent colour the mode chip is known to use, with a tolerance."""

    label: str
    rgb: tuple[int, int, int]
    tolerance: int = 40
    #: Fraction of the region's pixels that must match for the chip to count.
    min_coverage: float = 0.004


@dataclass(frozen=True, slots=True)
class ColourFinding:
    signature: ColourSignature
    coverage: float

    @property
    def matched(self) -> bool:
        return self.coverage >= self.signature.min_coverage


class ColourProbe:
    """Deterministic chip detector: counts pixels near a known accent colour.

    This is not a substitute for reading the label -- a colour is circumstantial.
    It is here as a cheap, offline, reproducible corroborating signal, and as the
    only probe that still works when every text dependency is missing (in which
    case it can corroborate but never confirm on its own).
    """

    name = "colour"

    def __init__(self, signatures: Sequence[ColourSignature], sample_step: int = 2) -> None:
        self.signatures = tuple(signatures)
        self.sample_step = max(1, sample_step)

    def read(self, screenshot: bytes, region: Region) -> tuple[bool, tuple[ColourFinding, ...]]:
        try:
            image = decode_png(screenshot)
        except Exception as exc:  # noqa: BLE001
            logger.info("colour probe could not decode screenshot: %s", exc)
            return False, ()
        return True, tuple(
            ColourFinding(sig, _coverage(image, region, sig, self.sample_step))
            for sig in self.signatures
        )


def _coverage(
    image: RgbImage, region: Region, signature: ColourSignature, step: int
) -> float:
    left, top, right, bottom = region.pixels(image.width, image.height)
    target_r, target_g, target_b = signature.rgb
    tol = signature.tolerance
    matched = 0
    sampled = 0
    for y in range(top, bottom, step):
        row = y * image.width
        for x in range(left, right, step):
            offset = (row + x) * 3
            sampled += 1
            if (
                abs(image.pixels[offset] - target_r) <= tol
                and abs(image.pixels[offset + 1] - target_g) <= tol
                and abs(image.pixels[offset + 2] - target_b) <= tol
            ):
                matched += 1
    return (matched / sampled) if sampled else 0.0
