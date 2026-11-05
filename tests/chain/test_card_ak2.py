"""Regression tests for the ward record fixes."""
import pytest

from wardos.chain.extract import to_candidates
from wardos.chain.normalize import split_flag


@pytest.mark.parametrize("text,number,shown,flag", [
    ("L -3.2", None, "-3.2", "L"),
    ("L -3.2", -3.2, "-3.2", "L"),
    ("-3.2 L", -3.2, "-3.2", "L"),
    ("H +3.2", 3.2, "+3.2", "H"),
    ("H-426", 426, "H-426", None),
    ("H. pylori positive", None, "H. pylori positive", None),
    ("H pylori positive", None, "H pylori positive", None),
    ("L 4-5 disc", None, "L 4-5 disc", None),
])
def test_flag_cleanup_preserves_signs_and_text_results(text, number, shown, flag):
    assert split_flag(text, number) == (shown, flag)


def test_extraction_keeps_negative_sign_when_model_omits_number():
    text = "Base excess L -3.2 mmol/L"
    raw = {"facts": [{"p": 1, "t": "lab_result", "k": "base excess", "v": "L -3.2 mmol/L", "q": text}]}
    (fact,) = to_candidates(raw, [{"id": "pg", "source_id": "s", "text": text}], admission={"id": "a"})
    assert fact["value"] == {"text": "-3.2 mmol/L", "number": None, "unit": None, "flag": "L"}
    assert fact["quote"] == text and fact["quote_found"]
