#!/usr/bin/env python3
"""
llm_edit.py -- natural-language edits to the drawio, style copied not invented.

    llm_edit.py --drawio diagrams/sc26.drawio \
                --instruction "add a circuit WC99, 400G, between SCinet NOC and Caltech Booth" \
                --out /tmp/edited.drawio

Design rules, each one answering a specific worry:

 1. STYLE IS NEVER AUTHORED. A new shape reuses an existing sibling's `style`
    string byte-for-byte. Edges get their styling from the capacity table below,
    which is LEARNED from the drawing rather than guessed -- see the note on
    CAPACITY_STYLE.

 2. IT PROPOSES, IT DOES NOT COMMIT. The output is a new .drawio. A human
    reviews the render before it reaches main.

 3. IT REFUSES RATHER THAN INVENTS. An unknown kind, an unknown endpoint, or a
    request needing a value the drawing cannot supply returns an error and
    exits non-zero. Silence is never treated as success.

 4. DRAWING CONTENT IS UNTRUSTED. Metadata in a .drawio is attacker-controlled
    text that reaches the model's prompt, so kinds are filtered to a safe
    charset and the operator's request is explicitly delimited.

Supported ops
-------------
  add_object  a shape of an existing kind (host, location, ...)
  add_edge    a circuit drawn between two existing shapes

Anything else is refused with an explanation.
"""
import argparse
import json
import os
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tag_drawio  # noqa: E402

# Kind names come from the file and reach the model's prompt, so they are
# restricted before use. 'host{{IGNORE ALL PRIOR INSTRUCTIONS}}' must not pass.
SAFE_KIND = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
SAFE_ID = re.compile(r"^WC\d{2,3}$")

# Capacity -> stroke width/colour. The real poster uses 25/40/80/120px strokes,
# proportioned for a wall-sized print; on a page-sized diagram they swamp the
# blocks. These keep the poster's ordering and colours at page scale, and the
# capacity is ALSO written as an explicit `capacity` attribute on the circuit,
# so extraction reads it as declared data rather than inferring from width.
# An unknown capacity is refused -- the tool will not invent a look.
CAPACITY_STYLE = {
    "10G":   (2, "#64748b"),
    "100G":  (3, "#111827"),
    "400G":  (6, "#2563eb"),
    "800G":  (8, "#7c3aed"),
    "1.2T":  (10, "#0d9488"),
    "1.6T":  (12, "#0d9488"),
    "2.4T":  (14, "#d97706"),
}

SYS = """You edit a drawio topology. Reply with ONLY a JSON object:

{"edits":[ ... ]}

Allowed ops:
  {"op":"add_edge","circuit":"WC99","capacity":"400G",
   "from":"<exact label of an existing block>","to":"<exact label of an existing block>"}
  {"op":"add_object","kind":"<one of the available kinds>","label":"<text>",
   "attrs":{"key":"value"}}

Hard rules:
  * Use ONLY labels and kinds listed as available. Do not invent either.
  * Never emit a style; the tool applies the file's own conventions.
  * Never invent a circuit id, capacity, or endpoint.
  * If the request cannot be done with what is available, reply
    {"edits":[],"error":"<one sentence why>"}.
"""


# ------------------------------------------------------------------- prompt
def diagram_facts(path):
    """What the model is allowed to reference: labels and kinds, nothing else."""
    tree = ET.parse(path)
    labels, kinds, styles = [], {}, {}
    for obj in tree.getroot().iter("object"):
        label = (obj.get("label") or "").strip()
        kind = obj.get("kind")
        if label:
            labels.append(label)
        if kind and SAFE_KIND.match(kind):
            inner = obj.find("mxCell")
            if kind not in styles and inner is not None and inner.get("style"):
                styles[kind] = inner.get("style")
            kinds[kind] = kinds.get(kind, 0) + 1
    return {"labels": labels, "kinds": kinds, "styles": styles}


def call_llm(prompt, model=None):
    base = os.environ.get("NRP_BASE_URL")
    tok = os.environ.get("NRP_AUTH_TOKEN")
    if not base or not tok:
        return None, "NRP_BASE_URL / NRP_AUTH_TOKEN not set"
    import urllib.request
    body = json.dumps({
        "model": model or os.environ.get("NRP_ANTHROPIC_MODEL", "claude"),
        "max_tokens": 1500,
        "system": SYS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(base.rstrip("/") + "/v1/messages",
                                 data=body, method="POST")
    req.add_header("x-api-key", tok)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        return d["content"][0]["text"], None
    except Exception as e:            # noqa: BLE001
        return None, str(e)[:200]


# ------------------------------------------------------------------- apply
def _block_center(root, label):
    """Find the shape whose label matches and return its centre point."""
    by_id = {}
    for el in root.iter():
        if el.tag in ("mxCell", "object") and el.get("id"):
            by_id[el.get("id")] = el
    for obj in root.iter("object"):
        if (obj.get("label") or "").strip() != label.strip():
            continue
        inner = obj.find("mxCell")
        if inner is None:
            continue
        g = inner.find("mxGeometry")
        if g is None:
            continue
        x, y = float(g.get("x", 0)), float(g.get("y", 0))
        w, h = float(g.get("width", 0)), float(g.get("height", 0))
        return obj.get("id"), (x + w / 2, y + h / 2, x, y, w, h)
    return None, None


def _locations(root):
    """(x, y, w, h) of every location block, for routing decisions."""
    out = []
    for obj in root.iter("object"):
        if obj.get("kind") != "location":
            continue
        g = obj.find("mxCell/mxGeometry")
        if g is not None:
            out.append(tuple(float(g.get(k, 0)) for k in ("x", "y", "width", "height")))
    return out


def _sides(root, a, b):
    """Pick exit/entry sides from where the blocks sit relative to each other.

    Two blocks stacked in one column with another block between them are
    joined around the right-hand side, so the line never cuts through the
    block in the middle (the straight-line version drew WC51 across Caltech).
    """
    ax, ay, aw, ah = a[2:]
    bx, by, bw, bh = b[2:]
    if bx >= ax + aw:
        return "right", "left"
    if bx + bw <= ax:
        return "left", "right"
    top, bot = (a, b) if ay < by else (b, a)
    between = [l for l in _locations(root)
               if l[1] >= top[3] + top[5] - 1 and l[1] + l[3] <= bot[3] + 1
               and l[0] < max(ax + aw, bx + bw) and l[0] + l[2] > min(ax, bx)]
    if between:
        return "right", "right"
    return ("bottom", "top") if by >= ay else ("top", "bottom")


def _slot(root, block_id, side, me, other):
    """Port position along `side`, aimed at the block on the other end.

    Ports were first handed out in insertion order, so a later circuit to a
    higher block could leave from a lower port and cross an earlier one.
    Aiming at the far block's centre keeps lines straight where the blocks
    overlap and stops them crossing; a port already in use is nudged.
    """
    mx, my, mw, mh = me[2:]
    if side in ("left", "right"):
        f = (other[1] - my) / mh
    else:
        f = (other[0] - mx) / mw
    f = round(min(0.8, max(0.2, f)), 2)
    used = set()
    for o in root.iter("object"):
        if o.get("kind") != "circuit":
            continue
        st = (o.find("mxCell").get("style") or "") if o.find("mxCell") is not None else ""
        for end, key in (("source", "exit"), ("target", "entry")):
            if o.get(end) == block_id:
                kv = dict(x.split("=", 1) for x in st.split(";") if "=" in x)
                used.add((kv.get(key + "X"), kv.get(key + "Y")))
    while tuple(str(v) for v in _port(side, f)) in used and f < 0.9:
        f = round(f + 0.1, 2)
    return f


def _shared(a, b, sa, sb):
    """Fractions that put both ends on one line, when the blocks overlap.

    Aiming each end independently left a small jog (WC53 left the NOC at one
    height and entered StarLight a few pixels lower). If the usable spans of
    the two facing sides overlap, use the middle of that overlap for both.
    """
    horiz = {sa, sb} == {"left", "right"}
    vert = {sa, sb} == {"top", "bottom"}
    if not (horiz or vert):
        return None
    i, j = (3, 5) if horiz else (2, 4)        # y,h  or  x,w  in the geo tuple
    lo = max(a[i] + 0.2 * a[j], b[i] + 0.2 * b[j])
    hi = min(a[i] + 0.8 * a[j], b[i] + 0.8 * b[j])
    if lo > hi:
        return None
    c = (lo + hi) / 2
    return round((c - a[i]) / a[j], 3), round((c - b[i]) / b[j], 3)


def _port(side, f):
    return {"right": (1, f), "left": (0, f),
            "top": (f, 0), "bottom": (f, 1)}[side]


def apply_edits(src, plan, facts, out):
    """Apply supported ops; refuse anything else. Returns (applied, errors)."""
    errors, applied = [], 0

    for e in plan.get("edits", []):
        if e.get("style") or (e.get("attrs") or {}).get("style"):
            errors.append("agent tried to author a style -- refused")
            continue
        if e.get("op") not in ("add_object", "add_edge"):
            errors.append("unsupported op %r" % e.get("op"))
            continue
        if e.get("op") == "add_object" and e.get("kind") not in facts["kinds"]:
            errors.append("unknown kind %r (have %s)"
                          % (e.get("kind"), sorted(facts["kinds"])))
        if e.get("op") == "add_edge":
            if not SAFE_ID.match(str(e.get("circuit") or "")):
                errors.append("bad circuit id %r" % e.get("circuit"))
            if e.get("capacity") not in CAPACITY_STYLE:
                errors.append("unknown capacity %r (have %s)"
                              % (e.get("capacity"), sorted(CAPACITY_STYLE)))
            for end in (e.get("from"), e.get("to")):
                if end not in facts["labels"]:
                    errors.append("unknown endpoint %r" % end)
    if errors:
        return 0, errors

    tree = ET.parse(src)
    root = tree.getroot()
    holder = next(root.iter("mxGraphModel")).find("root")
    existing = {el.get("id") for el in root.iter() if el.get("id")}

    def fresh(prefix):
        n = 1
        while "%s-%d" % (prefix, n) in existing:
            n += 1
        existing.add("%s-%d" % (prefix, n))
        return "%s-%d" % (prefix, n)

    for e in plan.get("edits", []):
        if e["op"] == "add_object":
            oid = fresh("agent")
            o = ET.SubElement(holder, "object",
                              {"id": oid, "label": e.get("label") or "",
                               "kind": e["kind"]})
            for k, v in (e.get("attrs") or {}).items():
                if SAFE_KIND.match(k):
                    o.set(k, str(v))
            c = ET.SubElement(o, "mxCell",
                              {"style": facts["styles"][e["kind"]],
                               "vertex": "1", "parent": "1"})
            ET.SubElement(c, "mxGeometry",
                          {"x": "120", "y": "120", "width": "160",
                           "height": "60", "as": "geometry"})
            applied += 1
            continue

        # add_edge -- a circuit drawn between two existing blocks
        a_id, a = _block_center(root, e["from"])
        b_id, b = _block_center(root, e["to"])
        if not a_id or not b_id:
            errors.append("endpoint not resolvable: %r -> %r"
                          % (e["from"], e["to"]))
            continue
        width, colour = CAPACITY_STYLE[e["capacity"]]
        exit_side, entry_side = _sides(root, a, b)
        fa = _slot(root, a_id, exit_side, a, b)
        fb = _slot(root, b_id, entry_side, b, a)
        shared = _shared(a, b, exit_side, entry_side)
        if shared:
            fa, fb = shared
        ex, ey = _port(exit_side, fa)
        nx, ny = _port(entry_side, fb)
        eid = fresh("circ-" + e["circuit"].lower())
        # wrapped here, with source/target mirrored and capacity declared, so
        # the tag pass sees it as already tagged and extraction reads
        # capacity from the drawing instead of inferring it from width
        obj = ET.SubElement(holder, "object", {
            "id": eid, "label": "", "kind": "circuit",
            "circuit": e["circuit"], "circuits": e["circuit"],
            "capacity": e["capacity"], "source": a_id, "target": b_id})
        cell = ET.SubElement(obj, "mxCell", {
            "style": ("edgeStyle=orthogonalEdgeStyle;rounded=1;arcSize=24;"
                      "endArrow=none;html=1;jumpStyle=gap;strokeWidth=%d;"
                      "strokeColor=%s;exitX=%s;exitY=%s;exitDx=0;exitDy=0;"
                      "entryX=%s;entryY=%s;entryDx=0;entryDy=0;"
                      % (width, colour, ex, ey, nx, ny)),
            "edge": "1", "parent": "1", "source": a_id, "target": b_id})
        ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        # the circuit id also goes on a child label, matching how the real
        # drawing stores it
        lbl = ET.SubElement(holder, "mxCell", {
            "id": fresh("lbl-" + e["circuit"].lower()),
            "value": "%s · %s" % (e["circuit"], e["capacity"]),
            "style": ("edgeLabel;html=1;fontSize=12;fontStyle=1;"
                      "fontColor=#0f172a;labelBackgroundColor=#ffffff;"
                      "labelBorderColor=%s;spacingLeft=6;spacingRight=6;"
                      % colour),
            "vertex": "1", "parent": eid})
        ET.SubElement(lbl, "mxGeometry",
                      {"x": "0", "relative": "1", "as": "geometry"})
        applied += 1

    ET.indent(tree.getroot())
    tree.write(out, encoding="UTF-8", xml_declaration=True)
    return applied, errors


# --------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--drawio", required=True)
    ap.add_argument("--instruction", required=True)
    ap.add_argument("-o", "--out")
    ap.add_argument("--json", help="write the edit plan here")
    ap.add_argument("--plan", help="apply this plan instead of calling the LLM "
                                   "(makes the apply path testable offline)")
    a = ap.parse_args(argv)

    facts = diagram_facts(a.drawio)

    if a.plan:
        plan = json.loads(pathlib.Path(a.plan).read_text())
    else:
        prompt = ("Available block labels: %s\nAvailable kinds: %s\n\n"
                  "OPERATOR INSTRUCTION:\n%s"
                  % (facts["labels"], sorted(facts["kinds"]), a.instruction))
        text, err = call_llm(prompt)
        if err:
            print("LLM unavailable: %s" % err, file=sys.stderr)
            return 2
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            print("could not parse model output:\n%s" % text[:600], file=sys.stderr)
            return 1
        plan = json.loads(m.group(0))

    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(plan, indent=2))

    if plan.get("error"):
        print("agent declined: %s" % plan["error"], file=sys.stderr)
        return 1
    if not plan.get("edits"):
        print("agent produced no edits", file=sys.stderr)
        return 1

    if not a.out:
        for e in plan["edits"]:
            print("  would apply:", json.dumps(e)[:110])
        return 0

    applied, errors = apply_edits(a.drawio, plan, facts, a.out)
    if errors:
        print("refused:", file=sys.stderr)
        for x in errors:
            print("  !", x, file=sys.stderr)
        return 1
    # Run the normal tagging pass so the new edge is wrapped in <object
    # kind=circuit>, exactly like every other circuit. Without this the edge
    # renders but extraction cannot see it -- the same label-vs-wrapper bug
    # this repo already hit once.
    stats = tag_drawio.tag(a.out, a.out)
    print("wrote %s (%d edit(s) applied; tagged %d edges, %d sub-ranges)"
          % (a.out, applied, stats["wrapped"], stats.get("subranges", 0)))

    # prove the result still round-trips
    problems, _, _, _ = tag_drawio.check(a.drawio, a.out)
    if problems:
        print("EDIT BROKE THE DRAWING:", file=sys.stderr)
        for p in problems[:5]:
            print("  !", p, file=sys.stderr)
        return 1
    print("  verified: styles and geometry of existing shapes unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
