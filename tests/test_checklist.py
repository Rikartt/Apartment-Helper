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


def node(expr):
    code = f"const c=require(process.argv[1]);console.log(JSON.stringify({expr}))"
    return json.loads(subprocess.run(["node", "-e", code, str(JS)], capture_output=True, text=True,
                                     check=True).stdout)


def test_flat_conditions_are_not_checklist_items():
    reqs = ["Lärmempfindliche Personen sind nicht geeignet", "noise-sensitive people unsuitable",
            "No pets", "Keine Haustiere", "Wohnung im 4. Stock ohne Lift",
            "Nichtraucher", "Nur Studierende", "Min. 6 Monate", "Referenzen erforderlich"]
    out = node("c.items({hard_requirements:%s}).map(i=>i.text)" % json.dumps(reqs))
    assert out == reqs[5:]


def test_asks_are_never_dropped():
    out = node("c.items({asks_applicant_to_include:['No pets please tell us about yourself']}).length")
    assert out == 1


def test_long_requirement_needs_only_a_third_of_keywords():
    req = "Wir suchen eine ordentliche, zuverlässige Person die regelmässig kocht und Gemeinschaft schätzt"
    d = "Ich bin ordentlich, zuverlässig und koche regelmässig."
    assert run([[req, "req", d]]) == [True]
    assert run([[req, "req", "Ich mag Velos."]]) == [False]


def test_extracted_fields_tick_date_and_duration():
    code = ("const c=require(process.argv[1]);const f=(t,d,e)=>c.check({text:t,kind:'ask'},d,e).ok;"
            "console.log(JSON.stringify([f('Einzugsdatum','Ich komme am 1.11.',{move_in:'2026-11-01'}),"
            "f('Einzugsdatum','Ich komme gern.',{move_in:'2026-11-01'}),"
            "f('Mietdauer','Gern unbefristet.',{duration:'permanent'}),"
            "f('Sprachkenntnisse','Ich spreche Deutsch (Muttersprache).',null)]))")
    out = json.loads(subprocess.run(["node", "-e", code, str(JS)], capture_output=True, text=True,
                                    check=True).stdout)
    assert out == [True, False, True, True]


def test_duration_phrasing_without_the_word_long_term():
    for d in ["Ich suche ein Zimmer für längere Zeit.", "I'd like to stay for a long time."]:
        assert run([["how long you want to stay", "ask", d]]) == [True], d
    assert run([["how long you want to stay", "ask", "Ich koche gern."]]) == [False]
