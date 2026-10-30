"""Child process for exact hidden-line removal of several views.

Exact HLR on freeform (B-spline) faces can run for many minutes inside
OpenCascade, where it cannot be interrupted. ``lineart.project_views`` runs it
here, with a time budget, and uses the polygonal algorithm for any view that is
not finished in time.

Input (stdin, JSON): {"parts": [{"name", "file" (.brep), "claimed"}], "keys": [...],
                      "include_hidden": bool, "include_smooth": bool, "out": dir}
Output: one ``<out>/<key>.json`` per finished view, written atomically.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    request = json.loads(sys.stdin.read())
    import cadquery as cq

    from .camera import STANDARD_VIEWS
    from .geometry import Part
    from .lineart import drawing_to_json, project_lines

    parts = [Part(name=row["name"], shape=cq.Shape.importBrep(row["file"]), claimed=bool(row["claimed"])) for row in request["parts"]]
    out = request["out"]
    for key in request["keys"]:
        drawing = project_lines(parts, STANDARD_VIEWS[key], include_hidden=bool(request.get("include_hidden")), include_smooth=bool(request.get("include_smooth", True)))
        tmp = os.path.join(out, f"{key}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(drawing_to_json(drawing), fh)
        os.replace(tmp, os.path.join(out, f"{key}.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
