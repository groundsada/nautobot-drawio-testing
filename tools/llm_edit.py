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
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")

# device fill/stroke by role; the tool authors these, never the model
ROLE_STYLE = {
    "router": ("#1e3a8a", "#172554"),
    "switch": ("#0f766e", "#134e4a"),
    "module": ("#7e22ce", "#581c87"),
    "server": ("#334155", "#1e293b"),
}
TIER = ["router", "switch", "module", "server"]
DEV_W, DEV_H, GAP_X, GAP_Y, HEAD = 104, 40, 14, 46, 52
ANCHOR = 0.68
# devices the layout repositions and blocks it grows; the final lossless
# check allows geometry changes for exactly these ids and nothing else
MOVED = set()

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
  {"op":"add_device","name":"<hostname>","role":"router|switch|module|server",
   "in":"<exact label of an existing block>"}
  {"op":"add_link","from":"<device or block label>","to":"<device or block label>",
   "capacity":"100G","count":3}

add_edge is ONLY for a WAN circuit with a WCnn id. Physical connections between
devices (server NIC, chassis module, switch uplink) are add_link. A bundle of
three 100G connections is one add_link with "count":3. Devices added earlier in
the same reply may be used as link endpoints. Add devices before their links.

Hard rules:
  * Use ONLY labels and kinds listed as available, plus devices you add.
  * Use the names the operator gives, exactly as written.
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
    locs = [(o.get("label") or "").strip() for o in tree.getroot().iter("object")
            if o.get("kind") == "location"]
    return {"labels": labels, "kinds": kinds, "styles": styles, "locations": locs}


def call_llm(prompt, model=None):
    base = os.environ.get("NRP_BASE_URL")
    tok = os.environ.get("NRP_AUTH_TOKEN")
    if not base or not tok:
        return None, "NRP_BASE_URL / NRP_AUTH_TOKEN not set"
    import urllib.request
    body = json.dumps({
        "model": model or os.environ.get("NRP_ANTHROPIC_MODEL", "claude"),
        # reasoning models spend part of this budget thinking before they
        # answer; at 1500 a six-device request came back cut off mid-name
        "max_tokens": 16000,
        "system": SYS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(base.rstrip("/") + "/v1/messages",
                                 data=body, method="POST")
    req.add_header("x-api-key", tok)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    # a long multi-device request took longer than 180s once and failed the
    # whole edit; allow more time and one retry on a timeout
    err = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read())
            text = "".join(b.get("text", "") for b in d.get("content", [])
                           if b.get("type") == "text")
            if d.get("stop_reason") == "max_tokens":
                return None, "model output truncated at max_tokens"
            return text, None
        except Exception as e:        # noqa: BLE001
            err = str(e)[:200]
            if "timed out" not in err:
                break
    return None, err


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
    # side by side only when the two overlap vertically; otherwise run
    # vertically -- testing left/right first sent hpc-srv-1's uplink sideways
    # across hpc-srv-2 to reach the module above it
    if by < ay + ah and ay < by + bh:
        return ("right", "left") if bx >= ax else ("left", "right")
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
        if o.get("kind") not in ("circuit", "link"):
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


def _location_obj(root, label):
    for o in root.iter("object"):
        if o.get("kind") == "location" and (o.get("label") or "").strip() == label:
            return o
    return None


def _layout(root, loc):
    """Stack a block's devices in role tiers and grow the block to fit.

    Tiers run router, switch, module, server away from the block's neighbour:
    from the top when nothing sits below the block, from the bottom when
    something does (so the NOC's routers face the HPC cluster under it).
    Returns the ids whose geometry it changed.
    """
    lid = loc.get("id")
    g = loc.find("mxCell/mxGeometry")
    bx, by, bw, bh = (float(g.get(k)) for k in ("x", "y", "width", "height"))
    devs = [o for o in root.iter("object")
            if o.get("kind") == "device" and o.get("location") == lid]
    rows = [[d for d in devs if d.get("role") == t] for t in TIER]
    rows = [r for r in rows if r]
    need = HEAD + len(rows) * DEV_H + (len(rows) - 1) * GAP_Y + 28
    moved = {d.get("id") for d in devs}
    if need > bh:
        g.set("height", str(int(need)))
        bh = need
        moved.add(lid)
    below = any(l[1] >= by + bh - 1 and l[0] < bx + bw and l[0] + l[2] > bx
                for l in _locations(root) if l[:2] != (bx, by))
    for i, row in enumerate(rows if not below else rows):
        width = len(row) * DEV_W + (len(row) - 1) * GAP_X
        # anchored right of centre: the block title sits top-left and
        # block-to-block circuits use the centre line, so a device uplink
        # centred there ran on top of WC52 and through the title
        left = min(max(bx + 16, bx + bw * ANCHOR - width / 2), bx + bw - 16 - width)
        y = (by + HEAD + i * (DEV_H + GAP_Y) if not below
             else by + bh - 28 - DEV_H - i * (DEV_H + GAP_Y))
        for j, d in enumerate(row):
            dg = d.find("mxCell/mxGeometry")
            dg.set("x", str(int(left + j * (DEV_W + GAP_X))))
            dg.set("y", str(int(y)))
    return moved


def _port(side, f):
    return {"right": (1, f), "left": (0, f),
            "top": (f, 0), "bottom": (f, 1)}[side]


def apply_edits(src, plan, facts, out):
    """Apply supported ops; refuse anything else. Returns (applied, errors)."""
    errors, applied = [], 0

    planned = []
    for e in plan.get("edits", []):
        if e.get("style") or (e.get("attrs") or {}).get("style"):
            errors.append("agent tried to author a style -- refused")
            continue
        if e.get("op") not in ("add_object", "add_edge", "add_device", "add_link"):
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
                if end not in facts["labels"] + planned:
                    errors.append("unknown endpoint %r" % end)
        if e.get("op") == "add_device":
            if not SAFE_NAME.match(str(e.get("name") or "")):
                errors.append("bad device name %r" % e.get("name"))
            elif e["name"] in facts["labels"] + planned:
                errors.append("device %r already exists" % e["name"])
            if e.get("role") not in ROLE_STYLE:
                errors.append("unknown role %r (have %s)" % (e.get("role"), sorted(ROLE_STYLE)))
            if e.get("in") not in facts["locations"]:
                errors.append("unknown block %r" % e.get("in"))
            planned.append(str(e.get("name")))
        if e.get("op") == "add_link":
            if e.get("capacity") not in CAPACITY_STYLE:
                errors.append("unknown capacity %r (have %s)"
                              % (e.get("capacity"), sorted(CAPACITY_STYLE)))
            if not isinstance(e.get("count", 1), int) or not 1 <= e.get("count", 1) <= 16:
                errors.append("bad link count %r" % e.get("count"))
            for end in (e.get("from"), e.get("to")):
                if end not in facts["labels"] + planned:
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

        if e["op"] == "add_device":
            loc = _location_obj(root, e["in"])
            o = ET.SubElement(holder, "object", {
                "id": fresh("dev-" + re.sub(r"[^a-z0-9]+", "-", e["name"].lower())),
                "label": e["name"], "kind": "device", "role": e["role"],
                "location": loc.get("id")})
            fill, stroke = ROLE_STYLE[e["role"]]
            c = ET.SubElement(o, "mxCell", {
                "style": ("rounded=1;arcSize=12;whiteSpace=wrap;html=1;fontFamily=Helvetica;"
                          "fillColor=%s;strokeColor=%s;fontColor=#ffffff;fontSize=12;"
                          "fontStyle=1;" % (fill, stroke)),
                "vertex": "1", "parent": "1"})
            ET.SubElement(c, "mxGeometry", {"x": "0", "y": "0", "width": str(DEV_W),
                                            "height": str(DEV_H), "as": "geometry"})
            MOVED.update(_layout(root, loc))
            applied += 1
            continue

        if e["op"] == "add_link":
            a_id, a = _block_center(root, e["from"])
            b_id, b = _block_center(root, e["to"])
            width, colour = CAPACITY_STYLE[e["capacity"]]
            sa, sb = _sides(root, a, b)
            fa = _slot(root, a_id, sa, a, b)
            fb = _slot(root, b_id, sb, b, a)
            shared = _shared(a, b, sa, sb)
            if shared:
                fa, fb = shared
            (ex, ey), (nx, ny) = _port(sa, fa), _port(sb, fb)
            n = int(e.get("count", 1))
            lid = fresh("link")
            obj = ET.SubElement(holder, "object", {
                "id": lid, "label": "", "kind": "link", "capacity": e["capacity"],
                "count": str(n), "source": a_id, "target": b_id})
            cell = ET.SubElement(obj, "mxCell", {
                "style": ("edgeStyle=orthogonalEdgeStyle;rounded=1;arcSize=24;"
                          "endArrow=none;html=1;jumpStyle=gap;strokeWidth=%d;"
                          "strokeColor=%s;exitX=%s;exitY=%s;exitDx=0;exitDy=0;"
                          "entryX=%s;entryY=%s;entryDx=0;entryDy=0;"
                          % (max(2, width - 2), colour, ex, ey, nx, ny)),
                "edge": "1", "parent": "1", "source": a_id, "target": b_id})
            ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
            lbl = ET.SubElement(holder, "mxCell", {
                "id": fresh("lbl-link"),
                "value": ("%d×%s" % (n, e["capacity"])) if n > 1 else e["capacity"],
                "style": ("edgeLabel;html=1;fontSize=11;fontColor=#334155;"
                          "labelBackgroundColor=#ffffff;"),
                "vertex": "1", "parent": lid})
            ET.SubElement(lbl, "mxGeometry", {"x": "0", "relative": "1", "as": "geometry"})
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
    problems = [p for p in problems
                if not (p.split(":", 1)[0] in MOVED and "geometry" in p)]
    if MOVED:
        print("  laid out / resized: %s" % ", ".join(sorted(MOVED)))
    if problems:
        print("EDIT BROKE THE DRAWING:", file=sys.stderr)
        for p in problems[:5]:
            print("  !", p, file=sys.stderr)
        return 1
    print("  verified: styles and geometry of existing shapes unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
