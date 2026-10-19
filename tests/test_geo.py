"""Point-in-polygon for the map's draw-area filter (frontend/geo.js) run under node; skipped without node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parent.parent / "frontend" / "geo.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

# a square around Zürich HB and an L-shaped (concave) area, as [lat, lng]
SQUARE = [[47.37, 8.53], [47.37, 8.55], [47.39, 8.55], [47.39, 8.53]]
ELL = [[47.36, 8.50], [47.36, 8.56], [47.38, 8.56], [47.38, 8.52], [47.40, 8.52], [47.40, 8.50]]


def node(expr, *args):
    code = f"const g=require(process.argv[1]);const a=JSON.parse(process.argv[2]);console.log(JSON.stringify({expr}))"
    return json.loads(subprocess.run(["node", "-e", code, str(JS), json.dumps(args)],
                                     capture_output=True, text=True, check=True).stdout)


def pip(points, poly):
    return node("a[0].map(([la,ln])=>g.pointInPolygon(la,ln,a[1]))", points, poly)


def test_inside_and_outside_a_square():
    assert pip([[47.38, 8.54], [47.375, 8.531]], SQUARE) == [True, True]
    assert pip([[47.36, 8.54], [47.38, 8.56], [47.40, 8.54], [47.38, 8.52]], SQUARE) == [False] * 4


def test_concave_polygon_notch_is_outside():
    # the notch of the L (north-east) is outside; both arms are inside
    assert pip([[47.39, 8.54], [47.37, 8.54], [47.39, 8.51]], ELL) == [False, True, True]


def test_winding_order_does_not_matter():
    assert pip([[47.38, 8.54], [47.36, 8.54]], SQUARE[::-1]) == [True, False]


def test_degenerate_input_is_outside():
    assert node("[g.pointInPolygon(47.38,8.54,[]), g.pointInPolygon(47.38,8.54,a[0].slice(0,2)),"
                " g.pointInPolygon(null,8.54,a[0]), g.pointInPolygon(47.38,8.54,null)]", SQUARE) == [False] * 4


def test_clean_polygon_drops_bad_points_and_short_lists():
    assert node("g.cleanPolygon(a[0])", SQUARE + [["x", 1], [91, 8.5], [47.38]]) == SQUARE
    assert node("[g.cleanPolygon(a[0].slice(0,2)), g.cleanPolygon('nope'), g.cleanPolygon(null)]", SQUARE) == [None] * 3
    assert node("g.cleanPolygon(a[0])[0]", [[47.123456789, 8.5], [47.2, 8.6], [47.3, 8.5]]) == [47.123457, 8.5]
