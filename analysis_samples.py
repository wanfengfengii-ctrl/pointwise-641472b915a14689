from copy import deepcopy

FILES = {
    "a": [
        {"id": 10, "station": "A", "t": 0, "reading": 0, "valid": True},
        {"id": 20, "station": "A", "t": 5, "reading": 10, "valid": True},
        {"id": 40, "station": "A", "t": 10, "reading": 0, "valid": True},
        {"id": 60, "station": "A", "t": 15, "reading": 5, "valid": False},
    ],
    "b": [
        {"id": 30, "station": "A", "t": 5, "reading": 4, "valid": True},
        {"id": 50, "station": "A", "t": 12, "reading": 4, "valid": True},
        {"id": 70, "station": "A", "t": 20, "reading": 10, "valid": True},
        {"id": 80, "station": "B", "t": 0, "reading": 100, "valid": True},
        {"id": 90, "station": "B", "t": 10, "reading": 100, "valid": True},
    ],
}
LAYOUT = [[["b", 1], ["a", 1], ["b", 0], ["a", 0]],
          [["a", 3], ["b", 2], ["a", 2], ["b", 3], ["b", 4]]]
NAMES = ("ties", "bridge", "layout", "conflict", "gap")


def sample(name):
    delete = {"op": "delete", "snapshot": 1, "file": "a", "pos": 1}
    prepare = {"op": "prepare", "job": "r", "inputs": ["a", "b"], "layout": LAYOUT}
    cases = {
        "ties": [delete, delete,
                 {"op": "rollback_delete", "snapshot": 1, "file": "b", "pos": 0},
                 {"op": "delete", "snapshot": 1, "file": "b", "pos": 0}],
        "bridge": [{"op": "delete", "snapshot": 1, "file": "a", "pos": 3},
                   {"op": "delete", "snapshot": 1, "file": "b", "pos": 0}],
        "layout": [prepare, {"op": "rollback_publish"}, {"op": "publish"}, delete,
                   {"op": "delete", "snapshot": 2, "file": "rewrite-r-0", "pos": 1},
                   {"op": "history", "snapshot": 1}],
        "conflict": [prepare, delete, {"op": "publish"}],
    }
    if name == "scale":
        return {"files": {"main": [FILES["a"][0], FILES["a"][2]],
                          "noise": [{"id": 1000 + i, "station": "Z", "t": i,
                                     "reading": i, "valid": True} for i in range(4096)]},
                "operations": [], "windows": [["A", -1000000, 1000000]]}
    if name == "gap":
        rows = [{"id": i + 1, "station": "A", "t": t, "reading": y, "valid": True}
                for i, (t, y) in enumerate([(0, 0), (10, 10), (21, 30)])]
        return {"files": {"edge": rows}, "operations": [],
                "windows": [["A", 5, 21], ["A", 10, 21], ["A", 7, 7], ["absent", 0, 30]]}
    return deepcopy({"files": FILES, "operations": cases[name],
                     "windows": [["A", 0, 20], ["A", 2, 8], ["B", 0, 20]]})
