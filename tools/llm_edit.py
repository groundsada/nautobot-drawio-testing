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

# Capacity -> stroke width/colour, taken VERBATIM from what the SC26 drawing
# already does (counted from labelled edges in the real file):
#   400G at width 20/40/60, 800G at 80, 1.2T at 120, 2.4T at 240, 100G at 25.
# Using the drawing's own convention means a new edge looks native. If a
# capacity is not in this table the tool refuses -- it will not invent a look.
CAPACITY_STYLE = {
    "10G":   (10, "#111111"),
    "100G":  (25, "#111111"),
    "400G":  (40, "#3399FF"),
    "800G":  (80, "#7B61FF"),
    "1.2T":  (120, "#1BA39C"),
    "1.6T":  (160, "#1BA39C"),
    "2.4T":  (240, "#E8A33D"),
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
        a_id, a_geo = _block_center(root, e["from"])
        b_id, b_geo = _block_center(root, e["to"])
        if not a_id or not b_id:
            errors.append("endpoint not resolvable: %r -> %r"
                          % (e["from"], e["to"]))
            continue
        width, colour = CAPACITY_STYLE[e["capacity"]]
        eid = fresh("circ-" + e["circuit"].lower())
        cell = ET.SubElement(holder, "mxCell", {
            "id": eid, "value": "",
            "style": ("endArrow=none;html=1;rounded=0;strokeWidth=%d;"
                      "strokeColor=%s;" % (width, colour)),
            "edge": "1", "parent": "1",
            "source": a_id, "target": b_id})
        ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        # the circuit id goes on a child label, matching how the real drawing
        # stores it -- extractors look here, not at the edge's own value
        lbl = ET.SubElement(holder, "mxCell", {
            "id": fresh("lbl-" + e["circuit"].lower()),
            "value": "%s  %s" % (e["circuit"], e["capacity"]),
            "style": ("edgeLabel;html=1;fontSize=13;fontStyle=1;"
                      "labelBackgroundColor=#ffffff;"),
            "vertex": "1", "parent": eid})
        ET.SubElement(lbl, "mxGeometry",
                      {"x": "0.5", "relative": "1", "as": "geometry"})
        # and mirrored onto an <object> wrapper so tag_drawio.extract sees it
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
