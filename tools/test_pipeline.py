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
