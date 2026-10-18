"""The draft-checklist heuristic (frontend/checklist.js) run under node; skipped without node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parent.parent / "frontend" / "checklist.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

DRAFT_DE = ("Hallo Anna, ich bin Rikard, 22 Jahre alt und studiere Elektrotechnik an der ETH. "
            "Ich kann ab 1.11. einziehen und moechte langfristig bleiben.")


def run(cases):
    code = ("const c=require(process.argv[1]);const cases=JSON.parse(process.argv[2]);"
            "console.log(JSON.stringify(cases.map(([t,k,d])=>c.check({text:t,kind:k},d).ok)))")
    out = subprocess.run(["node", "-e", code, str(JS), json.dumps(cases)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_covered_items_tick():
    res = run([[t, "ask", DRAFT_DE] for t in
               ["Einzugsdatum", "Alter", "Beruf / Studium", "gewünschte Mietdauer"]])
    assert res == [True, True, True, True]


def test_missing_items_stay_unticked():
    res = run([[t, "ask", DRAFT_DE] for t in ["Telefonnummer", "Foto", "Betreibungsauszug", "Nichtraucher"]])
    assert res == [False, False, False, False]


def test_date_formats_and_immediate():
    for d in ["I can move in on 2026-11-01.", "Available from 15 November.", "Ich kann ab sofort einziehen.",
              "from now on", "1.11.2026"]:
        assert run([["desired move-in date", "ask", d]]) == [True], d
    assert run([["desired move-in date", "ask", "I am a student and tidy."]]) == [False]


def test_number_matching_and_empty_draft():
    assert run([["Min. 6 Monate", "req", "Ich bleibe gerne 6 Monate."]]) == [True]
    assert run([["Min. 6 Monate", "req", ""]]) == [False]


def test_items_from_extraction():
    code = ("const c=require(process.argv[1]);console.log(JSON.stringify(c.items("
            "{asks_applicant_to_include:['a'],hard_requirements:['b']})))")
    out = json.loads(subprocess.run(["node", "-e", code, str(JS)], capture_output=True, text=True,
                                    check=True).stdout)
    assert out == [{"text": "a", "kind": "ask"}, {"text": "b", "kind": "req"}]
