"""Replay real Lake calls and full-scan queries; counts are observations, not an index."""
import json
import sys
from pathlib import Path
from lake import Lake
from objects import Objects
from analysis_ref import analyse
from analysis_samples import NAMES, sample


class Reads(Objects):
    def __init__(self):
        super().__init__()
        self.gets = []

    def get(self, fid):
        rows = super().get(fid)
        self.gets.append({"file": fid, "rows": len(rows)})
        return rows


class RollbackProbe(Exception):
    pass


def run(spec):
    objects = Reads()
    lake = Lake(objects=objects)
    sql = []
    lake.catalog.db.set_trace_callback(sql.append)
    plan = None

    def queries():
        return [analyse(lake, *window) for window in spec["windows"]]

    def step(operation):
        nonlocal plan
        objects.gets.clear()
        sql.clear()
        op = operation["op"]
        row = {"op": op}
        try:
            if op == "seed":
                row["snapshot"] = lake.seed(spec["files"])
            elif op in ("delete", "rollback_delete"):
                args = (operation["snapshot"], operation["file"], operation["pos"])
                if op == "delete":
                    row["snapshot"] = lake.delete(*args)
                else:
                    with lake.catalog.write():
                        lake.delete(*args)
                        row["inside"] = queries()
                        raise RollbackProbe()
            elif op == "prepare":
                plan = lake.prepare(operation["job"], operation["inputs"], operation["layout"])
                row["plan"] = plan
            elif op in ("publish", "rollback_publish"):
                if plan is None:
                    raise ValueError("NO_PLAN")
                if op == "publish":
                    row["snapshot"] = lake.publish(plan)
                else:
                    with lake.catalog.write():
                        lake.publish(plan)
                        row["inside"] = queries()
                        raise RollbackProbe()
            elif op == "history":
                row["history"] = lake.read(operation["snapshot"])
            else:
                raise ValueError("BAD_OPERATION")
        except RollbackProbe:
            row["error"] = "OBSERVER_ROLLBACK"
        except ValueError as exc:
            row["error"] = str(exc)
        row["operation_gets"] = list(objects.gets)
        row["transactions"] = [s for s in sql if s in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")]
        row["head"] = lake.catalog.head()
        objects.gets.clear()
        row["answers"] = queries()
        row["query_gets"] = list(objects.gets)
        return row

    try:
        return [step({"op": "seed"})] + [step(o) for o in spec["operations"]]
    finally:
        lake.catalog.db.close()


def main(argv):
    if argv == ["all"]:
        pack = {name: sample(name) for name in NAMES}
    elif len(argv) == 2 and argv[0] == "--input":
        pack = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    elif len(argv) == 1 and argv[0] in NAMES + ("scale",):
        pack = {argv[0]: sample(argv[0])}
    else:
        raise SystemExit("usage: analysis_probe.py all|ties|bridge|layout|conflict|gap|scale|--input FILE")
    for name, spec in pack.items():
        print(json.dumps({"case": name, "actual": run(spec)}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
