"""Calibration must see the page through OCR when the accessibility tree cannot."""

from __future__ import annotations

import io

import pytest

from egx_advisor.safety import vision

pytesseract = pytest.importorskip("pytesseract")
Image = pytest.importorskip("PIL.Image")


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_words_are_grouped_into_lines_and_weak_words_dropped(monkeypatch) -> None:
    data = {
        "text": ["Positions", "", "Market", "Value", "noise", "مراكز"],
        "conf": ["96", "-1", "91", "90", "12", "88"],
        "block_num": [1, 1, 2, 2, 2, 3],
        "par_num": [1, 1, 1, 1, 1, 1],
        "line_num": [1, 1, 1, 1, 1, 1],
    }
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *a, **k: data)
    lines, error = vision.ocr_screen_lines(_png())
    assert error is None
    assert lines == ["Positions", "Market Value", "مراكز"]


def test_a_missing_tesseract_binary_is_reported_not_raised(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    lines, error = vision.ocr_screen_lines(_png())
    assert lines == []
    assert error is not None and error.startswith("OCR failed")


def test_ragged_columns_from_tesseract_do_not_crash(monkeypatch) -> None:
    data = {"text": ["Buy", "Sell"], "conf": ["90"], "block_num": [1, 1],
            "par_num": [1, 1], "line_num": [1, 1]}
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *a, **k: data)
    lines, error = vision.ocr_screen_lines(_png())
    assert error is None
    assert lines == ["Buy"]
