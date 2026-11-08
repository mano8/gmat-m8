"""Write the subset of Lucide icons the application uses to ``static/vendor/lucide/icons.json``.

A maintainer tool, run by hand when an icon is added (nothing runs it at build time)::

    python scripts/vendor_icons.py <lucide-static>/package/icon-nodes.json name [name ...]

``icon-nodes.json`` comes from the ``lucide-static`` npm tarball recorded in
``static/vendor/README.md`` (compare its integrity with the registry's before using it). The
output keeps, per icon, the list of ``[tag, attributes]`` drawn in a 24x24 box; ``{% icon %}``
(``apps.core.templatetags.ui_tags``) builds the SVG from it with escaped attributes.
"""

import json
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "static" / "vendor" / "lucide" / "icons.json"
SHAPES = frozenset({"circle", "ellipse", "line", "path", "polygon", "polyline", "rect"})


def subset(nodes: dict[str, list[list[object]]], names: list[str]) -> dict[str, list[list[object]]]:
    """The icons called ``names``; an unknown name or an unexpected shape is an error."""
    missing = sorted(set(names) - nodes.keys())
    if missing:
        raise SystemExit(f"not in the Lucide set: {', '.join(missing)}")
    chosen = {name: nodes[name] for name in sorted(set(names))}
    for name, shapes in chosen.items():
        for tag, _attributes in shapes:
            if tag not in SHAPES:
                raise SystemExit(f"{name}: unexpected element <{tag}>")
    return chosen


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    nodes = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    TARGET.write_text(
        json.dumps(subset(nodes, argv[2:]), separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
