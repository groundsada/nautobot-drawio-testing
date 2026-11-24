#!/usr/bin/env python3
"""
nautobot_view.py -- what Nautobot holds, as Markdown with a Mermaid diagram.

    nautobot_view.py /tmp/result.json > /tmp/nautobot.md

Input is the RESULT that sync_nautobot.py --apply --result writes: counts plus
a `state` block read back from the database AFTER the write. Everything drawn
here comes from that read-back, never from the drawing, so the picture is
evidence of what Nautobot actually contains. A device missing from it is
missing from Nautobot. GitHub renders the ```mermaid block in PR comments.
"""
import json
import re
import sys
from collections import Counter, defaultdict


def nid(prefix, name):
    return prefix + re.sub(r"[^A-Za-z0-9]", "_", name)


def q(text):
    # Mermaid labels are quoted; a stray quote would end the label early
    return '"%s"' % str(text).replace('"', "'")


def render(result):
    st = result.get("state") or {}
    devs = st.get("devices", [])
    by_loc = defaultdict(list)
    for d in devs:
        by_loc[d["location"]].append(d)
    lines = ["flowchart LR"]
    for loc in st.get("locations", []):
        lines.append("  subgraph %s[%s]" % (nid("L_", loc), q(loc)))
        lines.append("    direction TB")
        for d in by_loc.get(loc, []):
            lines.append("    %s[%s]" % (nid("D_", d["name"]),
                                         q("%s<br/>%s" % (d["name"], d["role"]))))
        if not by_loc.get(loc):
            lines.append("    %s[ ]:::empty" % nid("E_", loc))
        lines.append("  end")
    # a 3x100G bundle is three cables; draw it as one line labelled 3x
    pairs = Counter()
    for c in st.get("cables", []):
        a, b = c.get("A", ""), c.get("B", "")
        if ":" in a and ":" in b:
            pairs[tuple(sorted((a.split(":")[0], b.split(":")[0])))] += 1
    for (a, b), n in sorted(pairs.items()):
        lines.append("  %s ---|%s| %s" % (nid("D_", a), q("%d cable%s" % (n, "s" * (n > 1))),
                                          nid("D_", b)))
    term = {c["label"]: c.get("A", "").split(":")[0] for c in st.get("cables", [])
            if " term" in c["label"]}
    for c in st.get("circuits", []):
        ends = c.get("ends") or {}
        a = term.get("%s termA" % c["cid"])
        z = term.get("%s termZ" % c["cid"])
        a = nid("D_", a) if a else (nid("L_", ends["A"]) if ends.get("A") else None)
        z = nid("D_", z) if z else (nid("L_", ends["Z"]) if ends.get("Z") else None)
        if a and z:
            lines.append("  %s -.-|%s| %s" % (a, q(c["cid"]), z))
    lines.append("  classDef empty fill:none,stroke:none")

    rows = ["| device | role | location | type | ports |", "|---|---|---|---|---|"]
    rows += ["| %s | %s | %s | %s | %d |" % (d["name"], d["role"], d["location"],
                                             d["type"], d["ports"]) for d in devs]
    circ = ["| circuit | provider | A | Z |", "|---|---|---|---|"]
    circ += ["| %s | %s | %s | %s |" % (c["cid"], c["provider"],
                                        (c.get("ends") or {}).get("A", "-"),
                                        (c.get("ends") or {}).get("Z", "-"))
             for c in st.get("circuits", [])]
    pr = result.get("pruned") or {}
    head = ("created %d, updated %d, unchanged %d; removed %d device(s), %d cable(s)"
            % (result.get("created", 0), result.get("updated", 0),
               result.get("unchanged", 0), pr.get("devices", 0), pr.get("cables", 0)))
    out = ["```mermaid", *lines, "```", "", head, ""]
    if devs:
        out += ["<details><summary>%d devices, %d cables</summary>" % (
            len(devs), len(st.get("cables", []))), "", *rows, "", "</details>", ""]
    if st.get("circuits"):
        out += circ
    return "\n".join(out)


def main(argv=None):
    argv = argv or sys.argv[1:]
    print(render(json.load(open(argv[0]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
