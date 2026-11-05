"""A patient whose name was not read shows 'Bed N' or 'New patient', never a made-up name."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).parents[2] / "wardos" / "chain" / "static" / "index.html"


@pytest.mark.skipif(not shutil.which("node"), reason="node missing")
def test_unread_name_shows_bed_or_new_patient():
    s = PAGE.read_text(encoding="utf-8")
    code = "\n".join(re.search(p, s).group(0) for p in (r'const NO_NAME = .*', r'const showName = .*', r'const firstName = .*'))
    js = code + """
console.log(JSON.stringify([showName("Name not read yet", 20), showName("Name not read yet", null), showName(null, 5), showName("Mr Tarun Kanwar", 3)]));"""
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["Bed 20", "New patient", "Bed 5", "Mr Tarun Kanwar"]


def test_ask_prompt_forbids_diagnosis_the_record_never_says():
    from wardos.chain.askai import PROMPT
    assert "Never state a diagnosis or condition" in PROMPT
    PROMPT.format(who="x", lines="y", question="z")
