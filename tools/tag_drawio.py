#!/usr/bin/env python3
"""
tag_drawio.py -- wrap drawio shapes in <object> metadata WITHOUT touching style.

    tag_drawio.py tag   <in.drawio> -o <out.drawio>   # propose tags
    tag_drawio.py read  <in.drawio>                   # extract tagged model
    tag_drawio.py check <in.drawio>                   # verify styles unchanged

The contract this file enforces
-------------------------------
Tom's diagram is the presentation artifact. Wrapping a shape in <object> must
change nothing about how it looks:

    <mxCell id="5" value="WC30" style="strokeWidth=20;strokeColor=#3399FF;..."
            vertex="1" parent="1">
      <mxGeometry .../>
    </mxCell>

becomes

    <object id="5" label="WC30" circuit="WC30" capacity="400G">
      <mxCell style="strokeWidth=20;strokeColor=#3399FF;..." vertex="1" parent="1">
        <mxGeometry .../>
      </mxCell>
    </object>

Only the wrapper is added. The `style` string is byte-identical, the geometry is
untouched, and the id moves from the mxCell to the object (as drawio requires).
`check` re-reads both files and asserts exactly that, so a bad edit cannot slip
through review.

Why <object> and not a style tweak: draw.io only supports custom metadata on an
<object> wrapper, and `placeholders="1"` on that wrapper lets a label contain
`%circuit%` / `%status%` which draw.io substitutes at render time. That is what
lets the poster show live data while remaining a hand-drawn poster.
"""
import argparse
import re
import sys
import xml.etree.ElementTree as ET

# Attributes drawio itself owns; never treat these as user metadata.
RESERVED = {"label", "id", "placeholders", "tooltip", "link", "tags",
            "vertex", "edge", "parent", "style", "value", "source", "target"}


# --------------------------------------------------------------- helpers
def style_capacity(style):
    """Infer capacity from a stroke style the way the drawing encodes it.

    Tom's file carries capacity both as a label and as stroke attributes. When
    a label is absent, the stroke is still evidence -- but it is a guess, so it
    is returned with a marker rather than as fact.
    """
    m = re.search(r"strokeWidth=([\d.]+)", style or "")
    if not m:
        return None
    try:
        w = float(m.group(1))
    except ValueError:
        return None
    # observed in the source drawing: 400G -> 20px, 100G -> thinner
    if w >= 18:
        return "400G"
    if w >= 8:
        return "200G"
    return "100G"


def label_text(cell):
    v = cell.get("value") or ""
    t = re.sub(r"<br\s*/?>|</div>|</p>", " ", v)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t.replace("\xa0", " ")).strip()


def find_circuit_ids(text):
    return sorted({"WC%02d" % int(x) for x in re.findall(r"\bWC\s?(\d{1,3})\b", text)})


# --------------------------------------------------------------- tag
def tag(src, dst):
    """Wrap the EDGE that carries a circuit label in <object> metadata.

    Critical detail discovered from the real file: no edge's own `value`
    contains a circuit id. All 56 circuit ids live on *child vertices* whose
    `parent` is the edge (drawio's edge-label form). An earlier version wrapped
    the label vertex, which lost `source`/`target` and extracted 0 hops.

    So: group children by parent edge, and if any child names a WCnn, wrap the
    PARENT edge. Labels are left exactly as they are.
    """
    tree = ET.parse(src)
    root = tree.getroot()
    stats = {"wrapped": 0, "labels": 0, "already": 0}

    for diagram in root.iter("diagram"):
        model = diagram.find("mxGraphModel")
        if model is None:
            continue
        holder = model.find("root")
        if holder is None:
            continue
        cells = [c for c in holder if c.tag in ("mxCell", "object")]
        by_id = {}
        for c in cells:
            cid = c.get("id") if c.tag == "object" else c.get("id")
            if cid:
                by_id[cid] = c

        # edge id -> set of circuit ids on its child labels
        edge_circuits = {}
        for c in cells:
            if c.tag != "mxCell":
                continue
            par = c.get("parent")
            if not par or par not in by_id:
                continue
            parent = by_id[par]
            inner = parent if parent.tag == "mxCell" else parent.find("mxCell")
            if inner is None or not inner.get("edge"):
                continue
            ids = find_circuit_ids(label_text(c))
            if ids:
                edge_circuits.setdefault(par, set()).update(ids)
                stats["labels"] += 1

        if not edge_circuits:
            continue

        for idx, c in enumerate(list(holder)):
            cid = c.get("id")
            if cid not in edge_circuits:
                continue
            if c.tag == "object":
                stats["already"] += 1
                continue
            if c.tag != "mxCell" or not c.get("edge"):
                continue

            ids = sorted(edge_circuits[cid])
            attrs = {
                "id": cid,
                "label": c.get("value") or "",
                "circuit": ids[0],
                "kind": "circuit",
                "circuits": ",".join(ids),
            }
            cap = style_capacity(c.get("style") or "")
            if cap:
                attrs["capacity_inferred"] = cap

            obj = ET.Element("object", {k: v for k, v in attrs.items() if v})
            inner = ET.SubElement(obj, "mxCell")
            for k, v in c.attrib.items():
                if k in ("id", "value"):
                    continue
                inner.set(k, v)
            for sub in list(c):
                inner.append(sub)
            for k in ("source", "target"):
                if c.get(k):
                    obj.set(k, c.get(k))

            holder.remove(c)
            holder.insert(idx, obj)
            stats["wrapped"] += 1

    tree.write(dst, encoding="UTF-8", xml_declaration=True)
    return stats


# --------------------------------------------------------------- read
def read(path):
    """Extract the tagged model: objects with metadata, plus tagless shapes."""
    tree = ET.parse(path)
    out = {"tagged": [], "untagged_edges": 0, "untagged_vertices": 0}
    for obj in tree.getroot().iter("object"):
        meta = {k: v for k, v in obj.attrib.items() if k not in RESERVED}
        inner = obj.find("mxCell")
        out["tagged"].append({
            "id": obj.get("id"),
            "label": obj.get("label"),
            "meta": meta,
            "style": inner.get("style") if inner is not None else None,
            "edge": obj.get("edge") or (inner.get("edge") if inner is not None else None),
            "source": obj.get("source") or (inner.get("source") if inner is not None else None),
            "target": obj.get("target") or (inner.get("target") if inner is not None else None),
        })
    for diagram in tree.getroot().iter("diagram"):
        model = diagram.find("mxGraphModel")
        if model is None:
            continue
        for cell in model.iter("mxCell"):
            if cell.get("edge"):
                out["untagged_edges"] += 1
            elif cell.get("vertex"):
                out["untagged_vertices"] += 1
    return out


# --------------------------------------------------------------- check
def check(original, tagged):
    """Prove tagging changed no style, geometry, or topology."""
    def snapshot(path):
        tree = ET.parse(path)
        cells = []
        for obj in tree.getroot().iter("object"):
            inner = obj.find("mxCell")
            if inner is None:
                continue
            geo = inner.find("mxGeometry")
            cells.append({
                "id": obj.get("id"),
                "style": inner.get("style"),
                "edge": inner.get("edge"),
                "vertex": inner.get("vertex"),
                "source": inner.get("source"),
                "target": inner.get("target"),
                "geo": tuple(sorted(geo.attrib.items())) if geo is not None else None,
                "label": obj.get("label"),
            })
        for cell in tree.getroot().iter("mxCell"):
            geo = cell.find("mxGeometry")
            cells.append({
                "id": cell.get("id"),
                "style": cell.get("style"),
                "edge": cell.get("edge"),
                "vertex": cell.get("vertex"),
                "source": cell.get("source"),
                "target": cell.get("target"),
                "geo": tuple(sorted(geo.attrib.items())) if geo is not None else None,
                "label": cell.get("value"),
            })
        return {c["id"]: c for c in cells if c["id"]}

    a, b = snapshot(original), snapshot(tagged)
    problems = []
    for cid, ca in a.items():
        cb = b.get(cid)
        if cb is None:
            problems.append("%s: shape disappeared" % cid)
            continue
        for field in ("style", "edge", "vertex", "source", "target", "geo"):
            if ca[field] != cb[field]:
                problems.append("%s: %s changed (%r -> %r)"
                                % (cid, field, ca[field], cb[field]))
        if (ca["label"] or "") != (cb["label"] or ""):
            problems.append("%s: label changed (%r -> %r)"
                            % (cid, ca["label"], cb["label"]))
    added = set(b) - set(a)
    return problems, len(a), len(b), sorted(added)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["tag", "read", "check"])
    ap.add_argument("src")
    ap.add_argument("other", nargs="?", help="tagged file (check mode)")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)

    if a.mode == "tag":
        if not a.out:
            ap.error("tag mode needs -o")
        try:
            st = tag(a.src, a.out)
        except ET.ParseError as e:
            print("not valid XML: %s\n  file: %s" % (e, a.src), file=sys.stderr)
            return 2
        print("wrapped %(wrapped)d edges (%(labels)d labels), already %(already)d" % st)
        return 0

    if a.mode == "read":
        import json
        r = read(a.src)
        print(json.dumps(r, indent=2)[:4000])
        print("...\n tagged=%d untagged_edges=%d untagged_vertices=%d"
              % (len(r["tagged"]), r["untagged_edges"], r["untagged_vertices"]))
        return 0

    if a.mode == "check":
        if not a.other:
            ap.error("check mode needs the tagged file as a second argument")
        problems, na, nb, added = check(a.src, a.other)
        print("original shapes: %d | tagged shapes: %d | added: %d"
              % (na, nb, len(added)))
        if problems:
            print("\nSTYLE/GEOMETRY CHANGED -- tagging is not lossless:")
            for p in problems[:20]:
                print("  !", p)
            return 1
        print("OK: every style, geometry, and topology attribute is unchanged")
        return 0


if __name__ == "__main__":
    sys.exit(main())
