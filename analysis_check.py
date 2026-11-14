import json
import random
import re
import sys
from fractions import Fraction
from pathlib import Path
from checks import check
from lake import Lake
from analysis_ref import G, analyse
from analysis_probe import run
from analysis_samples import NAMES, sample


def assert_value(answer, covered, numerator, denominator=1):
    value = Fraction(numerator, denominator)
    assert answer["covered"] == covered, answer
    assert answer["integral"] == [value.numerator, value.denominator], answer
    mean = value / covered if covered else None
    assert answer["mean"] == ([mean.numerator, mean.denominator] if mean is not None else None), answer


def midpoint_oracle(rows, station, left, right):
    # Independent small integer-grid oracle: each unit lies wholly in one segment or a gap.
    points = {}
    for row in rows:
        if row["station"] == station:
            points.setdefault(row["t"], []).append(row)
    points = {t: min(group, key=lambda row: row["id"]) for t, group in points.items()}
    covered, area = 0, Fraction(0)
    for unit in range(left, right):
        before = [t for t in points if t <= unit]
        after = [t for t in points if t >= unit + 1]
        if not before or not after:
            continue
        a, b = points[max(before)], points[min(after)]
        if a["valid"] and b["valid"] and b["t"] - a["t"] <= G:
            covered += 1
            area += a["reading"] + Fraction((b["reading"] - a["reading"]) *
                                           (2 * unit + 1 - 2 * a["t"]), 2 * (b["t"] - a["t"]))
    return covered, area


def baseline():
    check()
    cases = {name: run(sample(name)) for name in NAMES}
    ties = cases["ties"]
    for row, area in zip(ties, (54, 24, 24, 24, 4)):
        assert_value(row["answers"][0], 12, area)
    assert_value(ties[0]["answers"][1], 6, 42)
    assert_value(ties[1]["answers"][1], 6, 84, 5)
    assert_value(ties[3]["inside"][0], 12, 4)
    assert ties[3]["inside"][0]["snapshot"] == 3
    assert [row["head"] for row in ties] == [1, 2, 2, 2, 3]
    assert ties[2]["transactions"] == ["BEGIN IMMEDIATE", "COMMIT"]
    assert ties[3]["transactions"] == ["BEGIN IMMEDIATE", "ROLLBACK"]
    for row, area, covered in zip(cases["bridge"], (54, 110, 110), (12, 20, 20)):
        assert_value(row["answers"][0], covered, area)
        assert_value(row["answers"][2], 10, 1000)
    layout = cases["layout"]
    assert [row["head"] for row in layout] == [1, 1, 1, 2, 2, 3, 3]
    assert layout[2]["inside"][0]["snapshot"] == 2
    assert layout[4]["error"] == "STALE_LAYOUT"
    for row in layout[:5]:
        assert_value(row["answers"][0], 12, 54)
    assert_value(layout[5]["answers"][0], 12, 24)
    assert len(layout[6]["history"]["rows"]) == 9
    assert layout[6]["history"]["snapshot"] == 1
    conflict = cases["conflict"]
    assert conflict[-1]["error"] == "PUBLISH_CONFLICT"
    assert conflict[-1]["head"] == 2
    assert_value(conflict[-1]["answers"][0], 12, 24)
    gap = cases["gap"][0]["answers"]
    assert_value(gap[0], 5, 75, 2)
    for answer in gap[1:]:
        assert_value(answer, 0, 0)
    scale = run(sample("scale"))[0]
    assert_value(scale["answers"][0], 10, 0)
    assert sum(x["rows"] for x in scale["query_gets"]) == 4098
    rng = random.Random(71)
    rows = [{"id": i + 1, "station": rng.choice(["A", "B"]), "t": rng.randrange(21),
             "reading": rng.randrange(-10, 11), "valid": bool(rng.randrange(2))}
            for i in range(24)]
    lake = Lake()
    lake.seed({"random": rows})
    order = list(range(len(rows)))
    rng.shuffle(order)
    removed = set()
    for pos in order:
        removed.add(pos)
        lake.delete(1, "random", pos)
        visible = [r for i, r in enumerate(rows) if i not in removed]
        for station in ("A", "B"):
            covered, area = midpoint_oracle(visible, station, -1, 22)
            assert_value(analyse(lake, station, -1, 22), covered, area)
    lake.catalog.db.close()
    print("ANALYSIS_BASELINE_OK: exact integration, ties, gaps, rollback, rewrite and deletion oracle")
    return cases


def verify_document(text, cases, sources):
    match = re.search(r"<!-- interpolation-evidence -->\s*~~~json\s*\n(.*?)\n~~~", text, re.S)
    assert match, "missing interpolation-evidence JSON block"
    evidence = json.loads(match.group(1))
    assert set(evidence) == set(cases), "case names differ"
    for name, actual in cases.items():
        item = evidence[name]
        assert item["actual"] == actual, (name, "actual differs from replay")
        prediction = item["predicted"]
        assert len(prediction) == len(actual), (name, "step count")
        for predicted, row in zip(prediction, actual):
            assert predicted["op"] == row["op"] and predicted["head"] == row["head"], name
            assert predicted["answers"] == row["answers"], (name, "analysis changed")
            assert predicted.get("inside") == row.get("inside"), (name, "transaction view changed")
            for key in ("affected", "access"):
                assert isinstance(predicted[key], str) and predicted[key].strip(), (name, key)
    citations = re.findall(r"(lake\.py|catalog\.py|objects\.py|analysis_ref\.py):(\d+)", text)
    assert {name for name, _ in citations} == set(sources), "cite the four source modules"
    for name, line in citations:
        assert 1 <= int(line) <= len(sources[name].splitlines()), (name, line)
    return evidence


def main(argv):
    if argv not in (["baseline"], ["verify"]):
        raise SystemExit("usage: analysis_check.py baseline|verify")
    cases = baseline()
    if argv == ["verify"]:
        sources = {n: Path(n).read_text(encoding="utf-8")
                   for n in ("lake.py", "catalog.py", "objects.py", "analysis_ref.py")}
        verify_document(Path("DESIGN.md").read_text(encoding="utf-8"), cases, sources)
        print("DESIGN_REPLAY_OK: numerical evidence checked; index and transaction design require review")


if __name__ == "__main__":
    main(sys.argv[1:])
