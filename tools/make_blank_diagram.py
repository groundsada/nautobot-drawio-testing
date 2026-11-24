#!/usr/bin/env python3
"""
make_blank_diagram.py -- write the blank SC26 starting diagram.

    python3 tools/make_blank_diagram.py -o diagrams/sc26.drawio

Five location blocks, a title and a capacity legend; no circuits. Location
blocks are pre-wrapped in <object kind="location"> because this file is the
source, not a tag-pass output. The legend uses the same widths and colours as
llm_edit.CAPACITY_STYLE, and its swatches are plain vertices with no circuit
id, so extraction never mistakes them for circuits.
"""
import argparse
import pathlib
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from llm_edit import CAPACITY_STYLE  # noqa: E402

FONT = "fontFamily=Helvetica;"

# (id, label, x, y, w, h, fill, stroke)
BLOCKS = [
    ("loc-scinet-noc", "SCinet NOC", 40, 110, 380, 560, "#eff6ff", "#2563eb"),
    ("loc-hpc", "SCinet HPC Cluster", 40, 730, 380, 170, "#f5f3ff", "#7c3aed"),
    ("loc-starlight", "StarLight Booth 3651", 820, 110, 320, 150, "#fff7ed", "#ea580c"),
    ("loc-caltech", "Caltech Booth 5438", 820, 315, 320, 150, "#fff7ed", "#ea580c"),
    ("loc-ciena", "Ciena Booth 5035", 820, 520, 320, 150, "#fff7ed", "#ea580c"),
]


def build():
    mx = ET.Element("mxfile", {"host": "drawio", "pages": "1"})
    dia = ET.SubElement(mx, "diagram", {"name": "sc26", "id": "sc26-1"})
    model = ET.SubElement(dia, "mxGraphModel", {
        "grid": "1", "gridSize": "10", "guides": "1", "connect": "1",
        "arrows": "0", "page": "1", "pageScale": "1",
        "pageWidth": "1240", "pageHeight": "960", "background": "#ffffff"})
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    def vertex(cid, value, style, x, y, w, h, parent=root, wrap=None):
        host = parent
        attrs = {"style": FONT + style, "vertex": "1", "parent": "1"}
        if wrap is None:
            attrs.update(id=cid, value=value)
        else:
            host = ET.SubElement(parent, "object", dict(wrap, id=cid, label=value))
        c = ET.SubElement(host, "mxCell", attrs)
        ET.SubElement(c, "mxGeometry", {"x": str(x), "y": str(y), "width": str(w),
                                        "height": str(h), "as": "geometry"})

    vertex("title", "SC26 · SCinet WAN circuits",
           "text;html=1;fontSize=26;fontStyle=1;fontColor=#0f172a;align=left;"
           "verticalAlign=middle;", 40, 24, 700, 40)
    vertex("subtitle", "Source of truth for Nautobot. Edit on a PR with "
           "<b>/draw</b>, write with <b>/apply</b>.",
           "text;html=1;fontSize=13;fontColor=#64748b;align=left;"
           "verticalAlign=middle;", 40, 62, 760, 26)

    for bid, label, x, y, w, h, fill, stroke in BLOCKS:
        vertex(bid, label,
               "rounded=1;arcSize=6;whiteSpace=wrap;html=1;fillColor=%s;"
               "strokeColor=%s;strokeWidth=2;fontSize=17;fontStyle=1;"
               "fontColor=#0f172a;verticalAlign=top;spacingTop=14;" % (fill, stroke),
               x, y, w, h, wrap={"kind": "location", "site": label})

    # legend: one swatch per capacity, same width/colour as the edges
    lx, ly = 820, 730
    vertex("legend", "Capacity",
           "rounded=1;arcSize=6;html=1;fillColor=#ffffff;strokeColor=#cbd5e1;"
           "fontSize=13;fontStyle=1;fontColor=#334155;align=left;"
           "verticalAlign=top;spacingLeft=14;spacingTop=8;", lx, ly, 320, 170)
    for i, (cap, (w, colour)) in enumerate(
            sorted(CAPACITY_STYLE.items(), key=lambda kv: kv[1][0])):
        col, row = divmod(i, 4)
        x, y = lx + 16 + col * 150, ly + 42 + row * 30
        vertex("legend-%d" % i, "",
               "rounded=1;arcSize=50;html=1;fillColor=%s;strokeColor=none;" % colour,
               x, y + 10 - w / 2, 56, w)
        vertex("legend-t%d" % i, cap,
               "text;html=1;fontSize=12;fontColor=#334155;align=left;"
               "verticalAlign=middle;", x + 66, y, 70, 20)

    ET.indent(mx)
    return mx


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="diagrams/sc26.drawio")
    a = ap.parse_args(argv)
    pathlib.Path(a.out).write_bytes(
        ET.tostring(build(), encoding="UTF-8", xml_declaration=True))
    print("wrote", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
