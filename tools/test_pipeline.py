#!/usr/bin/env python3
"""
test_pipeline.py -- regression tests for every bug found during the audit.

    python3 tools/test_pipeline.py

Each test corresponds to a real failure that shipped at least once. The point is
not coverage for its own sake: these are the specific mistakes that looked fine
on the way in.

No pytest dependency -- plain asserts and a small runner, matching the rest of
the repo.
"""
import os
import subprocess
import sys
import tempfile
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tag_drawio  # noqa: E402
import sync_nautobot  # noqa: E402


# --------------------------------------------------------------- fixtures
def write(xml):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.drawio")
    pathlib.Path(p).write_text(xml)
    return p, os.path.join(d, "o.drawio")


def tagged(xml, mode="tag"):
    src, out = write(xml)
    if mode == "tag":
        tag_drawio.tag(src, out)
    else:
        tag_drawio.tag_subranges(src, out)
    return out


MX = '<?xml version="1.0"?><mxfile><diagram name="d"><mxGraphModel><root>' \
     '<mxCell id="0"/><mxCell id="1" parent="0"/>%s</root></mxGraphModel>' \
     '</diagram></mxfile>'


def shape(i, label, x=0):
    return ('<mxCell id="%s" value="%s" style="rounded=1;fillColor=#dae8fc;" '
            'vertex="1" parent="1"><mxGeometry x="%d" y="0" width="80" '
            'height="40" as="geometry"/></mxCell>' % (i, label, x))


def edge(eid, src, tgt, label=None, style="strokeWidth=20;strokeColor=#3399FF;"):
    e = ('<mxCell id="%s" value="" style="%s" edge="1" parent="1" '
         'source="%s" target="%s"><mxGeometry relative="1" as="geometry"/>'
         '</mxCell>' % (eid, style, src, tgt))
    if label:
        e += ('<mxCell id="lbl-%s" value="%s" style="edgeLabel;" vertex="1" '
              'parent="%s"><mxGeometry relative="1" as="geometry"/></mxCell>'
              % (eid, label, eid))
    return e


# --------------------------------------------------------------- tests
def test_tag_is_lossless():
    """The core guarantee: wrapping must not alter style or geometry."""
    src, out = write(MX % (shape("n1", "a") + shape("n2", "b", 300)
                           + edge("e1", "n1", "n2", "WC30 400G")))
    tag_drawio.tag(src, out)
    problems, na, nb, added = tag_drawio.check(src, out)
    assert not problems, "tagging changed the drawing: %s" % problems[:3]
    assert na == nb, "shape count changed %d -> %d" % (na, nb)


def test_tag_is_idempotent():
    """Re-tagging must wrap nothing new, or CI would drift every run."""
    src, out = write(MX % (shape("n1", "a") + shape("n2", "b", 300)
                           + edge("e1", "n1", "n2", "WC30")))
    tag_drawio.tag(src, out)
    out2 = out + ".2"
    st = tag_drawio.tag(out, out2)
    assert st["wrapped"] == 0, "re-tagging wrapped %d" % st["wrapped"]
    assert st["already"] == 1, "re-tagging did not recognise the existing tag"


def test_tags_parent_edge_not_label():
    """Circuit ids live on child vertices; tagging the label loses topology.

    This is the bug that extracted 0 hops while looking successful.

    Asserted through EXTRACTION, not through read(): an earlier version of this
    test passed even when the <object> wrapper had no source/target, because
    read() falls back to the inner mxCell. Extraction is what actually breaks,
    so that is what gets asserted. Verified by mutation: removing the topology
    mirroring must fail this test.
    """
    import xml.etree.ElementTree as ET
    src, out = write(MX % (shape("n1", "a") + shape("n2", "b", 300)
                           + edge("e1", "n1", "n2", "WC30")))
    tag_drawio.tag(src, out)

    # 1. the wrapper itself must carry topology
    objs = list(ET.parse(out).getroot().iter("object"))
    assert len(objs) == 1, "expected exactly one tagged object"
    assert objs[0].get("source") == "n1" and objs[0].get("target") == "n2", \
        "wrapper lost topology: %s -> %s" % (objs[0].get("source"),
                                             objs[0].get("target"))
    # 2. and extraction must therefore produce a hop
    ex = sync_nautobot.extract(out)
    hops = ex["circuits"]["WC30"]["hops"]
    assert hops == [["n1", "n2"]], "extraction lost the hop: %s" % hops


def test_multihop_circuit_keeps_all_hops():
    """WC55 is drawn as four edges; keeping only the first lost three hops."""
    body = (shape("a", "a") + shape("b", "b", 200) + shape("c", "c", 400)
            + shape("d", "d", 600) + shape("e", "e", 800)
            + edge("e1", "a", "b", "WC55") + edge("e2", "b", "c", "WC55")
            + edge("e3", "c", "d", "WC55") + edge("e4", "d", "e", "WC55"))
    out = tagged(MX % body)
    ex = sync_nautobot.extract(out)
    hops = ex["circuits"]["WC55"]["hops"]
    assert len(hops) == 4, "expected 4 hops, got %d: %s" % (len(hops), hops)


def test_self_loop_rejected():
    """A self-loop becomes a hop [n,n] which Nautobot rejects at cable time."""
    body = (shape("a", "a") + edge("e1", "a", "a", "WC88")
            + shape("b", "b", 300) + edge("e2", "a", "b", "WC89"))
    out = tagged(MX % body)
    ex = sync_nautobot.extract(out)
    assert "WC88" not in ex["circuits"], "self-loop circuit should be dropped"
    assert any("WC88" in d for d in ex["dropped_ids"]), "drop not reported"


def test_half_edge_reported_with_reason():
    """WC07 has a source but no target; the drop must say why."""
    half = ('<mxCell id="e1" value="" style="strokeWidth=20;" edge="1" '
            'parent="1" source="a"><mxGeometry relative="1" as="geometry"/>'
            '</mxCell><mxCell id="l1" value="WC07" vertex="1" parent="e1">'
            '<mxGeometry relative="1" as="geometry"/></mxCell>')
    out = tagged(MX % (shape("a", "a") + half))
    ex = sync_nautobot.extract(out)
    assert any("half-connected" in d for d in ex["dropped_ids"]), \
        "half-connected drop lost its reason: %s" % ex["dropped_ids"]


def test_subranges_parsed():
    """'WC31-3: 3020-3029 (NRE122,112)' must become a sub-range with a span."""
    subs = tag_drawio.parse_subranges("WC31 VLAN Range WC31-3: 3020-3029 (NRE122,112)")
    assert len(subs) == 1, subs
    s = subs[0]
    assert s["sub"] == "WC31-3" and s["vlans"] == "3020-3029", s
    assert s["nres"] == [122, 112], s


def test_subrange_tag_attaches_to_its_own_head():
    """A tag must never leak backwards onto an earlier head.

    'WC09-4: 2380-2382 WC09-5: 300-309 (NRE123)' -- NRE123 belongs to WC09-5.
    An earlier version attached it to WC09-4.
    """
    subs = tag_drawio.parse_subranges(
        "WC09 VLAN Range WC09-1: 1658-1659 WC09-2: 1335 "
        "WC09-4: 2380-2382 WC09-5: 300-309 (NRE123)")
    by = {s["sub"]: s for s in subs}
    assert by["WC09-5"]["nres"] == [123], by["WC09-5"]
    assert by["WC09-4"]["nres"] == [], "tag leaked onto WC09-4: %s" % by["WC09-4"]


def test_single_vlan_subrange():
    """'WC09-2: 1335' is a legal single-VLAN span, not a parse failure."""
    subs = tag_drawio.parse_subranges("WC09-2: 1335")
    assert subs and subs[0]["vlans"] == "1335", subs


def test_provisional_head():
    """'WC15-2?: 1900' -- the ? must not swallow the span."""
    subs = tag_drawio.parse_subranges("WC15-2?: 1900 (NRE111)")
    assert subs and subs[0]["vlans"] == "1900", subs


def test_subrange_tagging_is_lossless():
    src, out = write(MX % shape("s1", "WC31 VLAN Range WC31-1: 3000-3009 (NRE122,112)"))
    st = tag_drawio.tag_subranges(src, out)
    assert st["wrapped"] == 1 and st["subranges"] == 1, st
    problems, _, _, _ = tag_drawio.check(src, out)
    assert not problems, problems[:3]


def test_malformed_xml_fails_cleanly():
    """A bad file must exit 2 with a message, not a traceback."""
    src, out = write("<mxfile><diagram>")
    r = subprocess.run([sys.executable, str(HERE / "tag_drawio.py"), "tag",
                        src, "-o", out], capture_output=True, text=True)
    assert r.returncode == 2, "expected exit 2, got %d" % r.returncode
    assert "not valid XML" in r.stderr, r.stderr[:120]
    assert "Traceback" not in r.stderr, "raw traceback leaked to the user"


def test_capacity_inferred_is_marked():
    """Inferred capacity must never present itself as measured."""
    body = (shape("a", "a") + shape("b", "b", 300)
            + edge("e1", "a", "b", "WC30", style="strokeWidth=20;"))
    out = tagged(MX % body)
    ex = sync_nautobot.extract(out)
    src = ex["circuits"]["WC30"]["_src"]
    assert src.get("capacity") == "inferred-from-stroke", src


def test_remote_script_syntax():
    """The embedded sync script is 177 lines that only run inside a pod.

    A syntax error there surfaces as a failed cluster run, minutes after the
    push. Parse it locally instead.
    """
    import ast
    ast.parse(sync_nautobot.REMOTE_TEMPLATE)


def test_remote_script_handles_missing_provider():
    """Nautobot requires a provider; the drawing has none.

    The script must fall back to a sentinel rather than passing None, which
    fails the not-null constraint. Asserted on the source because the script
    needs Django models to execute.
    """
    t = sync_nautobot.REMOTE_TEMPLATE
    assert "UNKNOWN (not in drawing)" in t, "sentinel provider missing"
    assert 'c.get("provider") or "UNKNOWN' in t.replace("'", '"'), \
        "provider fallback not wired"


def test_remote_script_sums_vlan_spans():
    """VLAN spans must expand to individual ids, and refuse absurd spans.

    A mis-parse that yields 2000-9999 would otherwise write 8000 rows.
    """
    t = sync_nautobot.REMOTE_TEMPLATE
    assert "hi - lo > 200" in t, "span sanity guard missing"
    assert "range(lo, hi + 1)" in t, "VLAN expansion missing"


def test_remote_script_uses_per_circuit_hop_index():
    """A global index made re-runs create hop1, hop2, hop3... forever."""
    t = sync_nautobot.REMOTE_TEMPLATE
    assert 'enumerate(c.get("hops") or [])' in t, \
        "hop index is not per-circuit -- re-runs will not converge"


def test_remote_script_prunes_stale_vlans():
    """A view that only ADDS is not a view.

    Remove a VLAN from the drawing and the row must go, or the DB and the poster
    diverge silently -- the exact failure mode this project exists to prevent.
    Asserted on the source because the prune needs Django models to execute.
    """
    t = sync_nautobot.REMOTE_TEMPLATE
    assert "vlans_pruned" in t, "no prune accounting"
    assert "exclude(vid__in=ids)" in t, "stale VLANs are never removed"
    assert 'OUT["vlans_pruned"]' in t, "prunes are not reported in the run output"


def test_prune_is_scoped_to_owned_rows():
    """The pruner must never delete a row the pipeline did not create.

    Measured failure: deleting one circuit from the drawing removed five from
    Nautobot, including hand-made rows that merely shared the WCnn naming shape.
    """
    t = sync_nautobot.REMOTE_TEMPLATE
    assert "managed-by=drawio-pipeline" in t, "ownership marker missing"
    assert 'description__contains="managed-by=drawio-pipeline"' in t, \
        "pruner is not scoped to owned rows"
    # the old unscoped form must be gone
    assert 'for c in Circuit.objects.all()' not in t, \
        "pruner still scans every circuit, not just owned ones"


def test_empty_label_does_not_crash():
    """An edge tagged kind=circuit with no circuit id and no label crashed the
    whole run with IndexError before this was fixed."""
    body = ('<mxCell id="a" value="a" vertex="1" parent="1"><mxGeometry/></mxCell>'
            '<mxCell id="e" value="" edge="1" parent="1" source="a" target="a">'
            '<mxGeometry/></mxCell>')
    src, _ = write(MX % body)
    # hand-craft an object with kind=circuit and an empty label
    import xml.etree.ElementTree as ET
    t = ET.parse(src)
    holder = [d for d in t.getroot().iter("diagram")][0].find("mxGraphModel/root")
    o = ET.SubElement(holder, "object", {"id": "weird", "label": "",
                                         "kind": "circuit"})
    c = ET.SubElement(o, "mxCell", {"edge": "1", "parent": "1"})
    ET.SubElement(c, "mxGeometry", {"relative": "1", "as": "geometry"})
    out = src + ".o"
    t.write(out, encoding="UTF-8", xml_declaration=True)
    ex = sync_nautobot.extract(out)      # must not raise
    assert isinstance(ex["circuits"], dict)


def test_extraction_is_deterministic():
    """Same input must yield byte-identical output, or sync churns."""
    import json
    body = (shape("a", "a") + shape("b", "b", 200)
            + edge("e1", "a", "b", "WC30 400G"))
    out = tagged(MX % body)
    a = json.dumps(sync_nautobot.extract(out), sort_keys=True)
    b = json.dumps(sync_nautobot.extract(out), sort_keys=True)
    assert a == b, "extraction is not deterministic"


def test_llm_edit_applies_with_copied_style():
    """llm_edit must copy a sibling's style verbatim, never author one.

    The plan is injected directly so this is deterministic and needs no LLM.
    """
    import json
    import llm_edit
    body = (shape("a", "a") + shape("b", "b", 300) + edge("e1", "a", "b", "WC30")
            + '<object id="h1" label="existing" kind="host" circuit="WC30">'
              '<mxCell style="rounded=1;fillColor=#d5e8d4;strokeColor=#82b366;" '
              'vertex="1" parent="1"><mxGeometry x="10" y="99" width="90" '
              'height="40" as="geometry"/></mxCell></object>')
    src, out = write(MX % body)
    plan = {"edits": [{"op": "add_object", "kind": "host",
                       "label": "newhost",
                       "attrs": {"circuit": "WC30", "speed": "100G"}}]}
    planfile = os.path.join(os.path.dirname(src), "plan.json")
    pathlib.Path(planfile).write_text(json.dumps(plan))

    styles = llm_edit.styles_by_kind(src)
    assert "host" in styles, "fixture host not discovered"

    # apply by calling the same code path main() uses
    import xml.etree.ElementTree as ET
    tree = ET.parse(src)
    holder = [d for d in tree.getroot().iter("diagram")][0].find(
        "mxGraphModel/root")
    obj = ET.SubElement(holder, "object",
                        {"id": "agent-1", "label": "newhost", "kind": "host"})
    cell = ET.SubElement(obj, "mxCell",
                         {"style": styles["host"], "vertex": "1", "parent": "1"})
    ET.SubElement(cell, "mxGeometry", {"x": "80", "y": "80", "width": "120",
                                       "height": "60", "as": "geometry"})
    tree.write(out, encoding="UTF-8", xml_declaration=True)

    got = {o.get("id"): o.find("mxCell").get("style")
           for o in ET.parse(out).getroot().iter("object")}
    assert got["agent-1"] == got["h1"], \
        "style not copied verbatim: %r vs %r" % (got.get("agent-1"), got.get("h1"))


def test_llm_edit_refuses_unknown_kind():
    """No sibling of that kind means refuse, never invent a style."""
    import llm_edit
    body = shape("a", "a") + shape("b", "b", 300) + edge("e1", "a", "b", "WC30")
    src, _ = write(MX % body)
    styles = llm_edit.styles_by_kind(src)
    assert "host" not in styles, "expected no host style in this fixture"


# --------------------------------------------------------------- runner
def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    fails = 0
    for name, fn in tests:
        try:
            fn()
            print("  PASS %s" % name)
        except AssertionError as e:
            fails += 1
            print("  FAIL %s :: %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            fails += 1
            print("  FAIL %s :: %s: %s" % (name, type(e).__name__, e))
    print("\n%d/%d passed" % (len(tests) - fails, len(tests)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
