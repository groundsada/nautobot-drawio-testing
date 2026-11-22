#!/usr/bin/env python3
"""
llm_edit.py -- natural-language edits to the drawio, style copied not invented.

    llm_edit.py --drawio diagrams/topology.drawio --instruction "add a host foo
                 behind scn-sw-arst-2 on WC30 at 100G" -o /tmp/out.drawio

Three things make this safe rather than magical:

 1. STYLE IS COPIED. When the agent needs a new shape it locates an existing
    shape of the same `kind` in the same file and reuses its `style` attribute
    verbatim. It never authors a style string. This is why "wouldn't know how to
    draw another blob" is not a blocker -- it imitates.

 2. IT PROPOSES, IT DOES NOT COMMIT. Output is a new .drawio plus a machine
    readable diff summary. A human reviews the visual before it reaches main.

 3. IT REFUSES ON AMBIGUITY. If no sibling of the requested kind exists, it
    stops and says so rather than inventing one.

The LLM endpoint is the NRP Anthropic-compatible route, configured through
NRP_BASE_URL / NRP_AUTH_TOKEN (see docs/SECURITY.md). Neither is ever logged.
"""
import argparse, json, os, pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tag_drawio  # noqa: E402

SYS = """You edit drawio XML. Output ONLY a JSON object with keys:
  {"edits":[{"op":"add_object","kind":"host","near":"<existing label>",
             "attrs":{"circuit":"WCnn","speed":"100G","kind":"host"},
             "label":"<text>"}]}
Rules:
  * Never output a `style` string; the tool copies one from a sibling.
  * Never invent a circuit id, node id, or capacity.
  * If the request needs something not in the file, output {"edits":[],"error":"why"}.
"""


def call_llm(prompt):
    base = os.environ.get("NRP_BASE_URL")
    tok = os.environ.get("NRP_AUTH_TOKEN")
    if not base or not tok:
        return None, "NRP_BASE_URL / NRP_AUTH_TOKEN not set"
    import urllib.request
    body = json.dumps({
        "model": os.environ.get("NRP_ANTHROPIC_MODEL", "claude"),
        "max_tokens": 1500,
        "system": SYS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(base.rstrip("/") + "/v1/messages", data=body,
                                 method="POST")
    req.add_header("x-api-key", tok)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        return d["content"][0]["text"], None
    except Exception as e:
        return None, str(e)[:200]


def styles_by_kind(path):
    """Map kind -> an existing style string, harvested from the file."""
    out = {}
    model = tag_drawio.read(path)
    for t in model["tagged"]:
        k = t["meta"].get("kind")
        if k and t["style"] and k not in out:
            out[k] = t["style"]
    # untagged vertices: fall back to any rounded box as the 'box' style
    import xml.etree.ElementTree as ET
    for c in ET.parse(path).getroot().iter("mxCell"):
        if c.get("vertex") and c.get("style") and "box" not in out:
            out["box"] = c.get("style")
            break
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--drawio", required=True)
    ap.add_argument("--instruction", required=True)
    ap.add_argument("-o", "--out")
    ap.add_argument("--json", help="write the edit plan here")
    a = ap.parse_args(argv)

    styles = styles_by_kind(a.drawio)
    prompt = ("File kinds available: %s\nInstruction: %s"
              % (sorted(styles), a.instruction))
    text, err = call_llm(prompt)
    if err:
        print("LLM unavailable: %s" % err, file=sys.stderr)
        print("This tool needs NRP_BASE_URL and NRP_AUTH_TOKEN in the env.")
        return 2
    try:
        plan = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except Exception:
        print("could not parse LLM output:\n%s" % text[:800], file=sys.stderr)
        return 1

    if plan.get("error"):
        print("agent declined: %s" % plan["error"], file=sys.stderr)
        return 1

    # refuse rather than invent when a needed style is missing
    missing = [e.get("kind") for e in plan.get("edits", [])
               if e.get("kind") and e["kind"] not in styles]
    if missing:
        print("no sibling style for kind(s) %s -- refusing to invent one"
              % sorted(set(missing)), file=sys.stderr)
        print("available: %s" % sorted(styles), file=sys.stderr)
        return 1

    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(plan, indent=2))
    print("plan: %d edit(s)" % len(plan.get("edits", [])))
    for e in plan.get("edits", []):
        print("  %s -> style copied from kind=%r" % (e.get("label"), e.get("kind")))

    if not a.out:
        return 0

    # Apply the edits. Only `add_object` is implemented; anything else is
    # refused rather than silently ignored, because a tool that accepts an edit
    # and writes an unchanged file is worse than one that does not exist.
    import xml.etree.ElementTree as ET
    unsupported = [e for e in plan.get("edits", [])
                   if e.get("op") != "add_object"]
    if unsupported:
        print("unsupported op(s): %s -- refusing rather than half-applying"
              % sorted({e.get("op") for e in unsupported}), file=sys.stderr)
        return 1

    tree = ET.parse(a.drawio)
    root = tree.getroot()
    applied = 0
    for e in plan.get("edits", []):
        kind = e.get("kind")
        style = styles.get(kind)
        if not style:
            print("no style for kind %r" % kind, file=sys.stderr)
            return 1
        for diagram in root.iter("diagram"):
            model = diagram.find("mxGraphModel")
            if model is None:
                continue
            holder = model.find("root")
            if holder is None:
                continue
            existing = {c.get("id") for c in holder}
            n = 1
            while "agent-%d" % n in existing:
                n += 1
            new_id = "agent-%d" % n
            obj = ET.SubElement(holder, "object", {
                "id": new_id,
                "label": e.get("label") or "",
                "placeholders": "1",
            })
            for k, v in (e.get("attrs") or {}).items():
                obj.set(k, str(v))
            cell = ET.SubElement(obj, "mxCell", {
                "style": style,          # copied verbatim from a sibling
                "vertex": "1",
                "parent": "1",
            })
            ET.SubElement(cell, "mxGeometry", {
                "x": "80", "y": "80", "width": "120", "height": "60",
                "as": "geometry",
            })
            applied += 1
            break
    tree.write(a.out, encoding="UTF-8", xml_declaration=True)
    print("wrote %s (%d object(s) added, style copied from siblings)"
          % (a.out, applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
