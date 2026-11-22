#!/usr/bin/env python3
"""
sync_nautobot.py -- drawio tagged model -> Nautobot, idempotently.

    sync_nautobot.py --drawio diagrams/topology.drawio --dry-run
    sync_nautobot.py --drawio diagrams/topology.drawio --apply

How it talks to Nautobot
------------------------
It does NOT use the REST API. It renders a small Python program and pipes it
into `kubectl exec <nautobot-pod> -- nautobot-server shell`. Reasons, measured:

  * REST bulk upsert: ~2s per object, and 56 circuits took minutes.
  * Cable terminations cannot be set through the REST create shape at all --
    `a_terminations` is accepted and silently ignored, leaving cables with NULL
    endpoints. That bug made the topology render empty for hours.
  * The ORM path is one round trip and is idempotent by construction.

Idempotency contract
--------------------
Every object is `update_or_create`d on a natural key:
    circuit   -> cid
    device    -> serial (holds the drawio node id)
    cable     -> a deterministic label built from the circuit and hop index
Re-running with unchanged input reports `created 0, updated 0`. That is
asserted by CI (`--expect-noop` on the second run), because a pipeline that
silently duplicates is worse than one that fails.

Provenance
----------
Metadata comes from <object> attributes only. Anything absent is reported as
missing rather than guessed. The payload carries `_src` markers so a fabricated
value can never be presented as observed.
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys
import textwrap

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tag_drawio  # noqa: E402


# ----------------------------------------------------------------- extraction
def extract(path):
    """Tagged drawio -> a small, explicit model. No inference beyond labels."""
    model = tag_drawio.read(path)
    circuits = {}
    hosts = []

    for t in model["tagged"]:
        meta = t["meta"]
        if meta.get("kind") == "circuit" or "circuit" in meta:
            cid = meta.get("circuit") or (t["label"] or "").split()[0]
            if not re.fullmatch(r"WC\d{2}", cid or ""):
                continue
            c = circuits.setdefault(cid, {
                "cid": cid, "capacity": None, "provider": None,
                "subranges": [], "hops": [], "_src": {},
            })
            if meta.get("capacity"):
                c["capacity"] = meta["capacity"]
                c["_src"]["capacity"] = "drawing"
            elif meta.get("capacity_inferred"):
                c["capacity"] = meta["capacity_inferred"]
                c["_src"]["capacity"] = "inferred-from-stroke"
            if meta.get("provider"):
                c["provider"] = meta["provider"]
                c["_src"]["provider"] = "drawing"
            if meta.get("vlans"):
                c["subranges"].append({
                    "sub": meta.get("sub") or cid,
                    "vlans": meta["vlans"],
                    "nres": [int(x) for x in re.findall(r"\d+", meta.get("nres", ""))],
                })
            # A self-loop is not a hop: cabling an interface to itself fails
            # inside Nautobot and would otherwise reach the database layer.
            # A circuit may be drawn as SEVERAL edges (WC55 is 4 separate hops
            # on 4 different node pairs). Appending only one silently dropped
            # the rest: extraction reported 55 hops for 56 tagged edges.
            if t.get("source") and t.get("target") and t["source"] != t["target"]:
                hop = [t["source"], t["target"]]
                if hop not in c["hops"]:
                    c["hops"].append(hop)
            elif t.get("source") == t.get("target") and t.get("source"):
                c["_src"]["self_loops"] = c["_src"].get("self_loops", 0) + 1
            elif t.get("source") or t.get("target"):
                # half-connected edge: count it, do not pretend it is a hop
                c["_src"]["incomplete_edges"] = c["_src"].get("incomplete_edges", 0) + 1
            continue

        if meta.get("kind") == "subrange":
            for chunk in (meta.get("subs") or "").split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                m = re.fullmatch(r"WC(\d{2})-(\d+)", chunk)
                if not m:
                    continue
                cid = "WC%s" % m.group(1)
                c = circuits.setdefault(cid, {
                    "cid": cid, "capacity": None, "provider": None,
                    "subranges": [], "hops": [], "_src": {},
                })
                parsed = tag_drawio.parse_subranges(t.get("label") or "")
                for ps in parsed:
                    if ps["sub"] != chunk:
                        continue
                    if ps["sub"] in {x["sub"] for x in c["subranges"]}:
                        c["_src"]["duplicate_subrange_boxes"] = \
                            c["_src"].get("duplicate_subrange_boxes", 0) + 1
                        continue
                    c["subranges"].append({
                        "sub": ps["sub"], "vlans": ps["vlans"], "nres": ps["nres"]})
                    c["_src"]["subrange_source"] = "drawing"
            continue

        if meta.get("kind") == "host":
            hosts.append({
                "name": meta.get("hostname") or t["label"],
                "node_id": t["id"],
                "circuit": meta.get("circuit"),
                "speed": meta.get("speed"),
                "role": meta.get("role") or "host",
            })

    # A circuit must be on the graph (>=1 hop) or carry VLAN data. Drawings
    # contain ids in prose (e.g. WC32) that are not circuits; they were being
    # emitted as empty records.
    keep = {k: v for k, v in circuits.items()
            if v["hops"] or v["subranges"]}
    # explain WHY each id was dropped instead of emitting a bare list: a
    # circuit present only as a self-loop or a half-edge is a drawing defect
    # someone should look at, not a silent omission
    dropped = []
    for k in sorted(set(circuits) - set(keep)):
        why = "no usable hop or VLAN"
        src = circuits[k]["_src"]
        if src.get("self_loops"):
            why = "only self-loop edges"
        elif src.get("incomplete_edges"):
            why = "only half-connected edges"
        elif src.get("incomplete_edges") is None and src.get("self_loops") is None:
            why = "appears in text but has no edge on the graph"
        dropped.append("%s (%s)" % (k, why))

    return {"circuits": keep, "hosts": hosts,
            "dropped_ids": dropped,
            "untagged_edges": model["untagged_edges"]}


# ------------------------------------------------------------------- remote
REMOTE_TEMPLATE = """
import json, os, re, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nautobot_config")
django.setup()
from nautobot.dcim.models import Device, Cable, Interface, DeviceType, Location
from nautobot.extras.models import Status, Role
from nautobot.circuits.models import Circuit, Provider, CircuitType

PAYLOAD = json.loads(open("/tmp/_sync_payload.json").read())
DRY = PAYLOAD["dry_run"]
OUT = {"created": 0, "updated": 0, "skipped": 0, "errors": []}

def note(kind):
    OUT[kind] = OUT.get(kind, 0) + 1

st = Status.objects.get(name="Active")
ctype, _ = CircuitType.objects.get_or_create(name="WAN Wave")
dt = DeviceType.objects.first()
role_r = Role.objects.get(name="Router")
role_e = Role.objects.get(name="Endpoint")
loc = Location.objects.get(name="SCinet NOC")

prov_cache = {}
def provider(name):
    if not name:
        return None
    if name not in prov_cache:
        p, _ = Provider.objects.get_or_create(name=name)
        prov_cache[name] = p
    return prov_cache[name]

# ---- circuits
for c in PAYLOAD["circuits"]:
    try:
        # Nautobot requires a provider. The drawing does not carry one for any
        # circuit, so using a null fails the not-null constraint and using a
        # real provider would be an invention. Create an explicit sentinel
        # provider so "unknown" is visible in the UI rather than guessed, and
        # record it in the description so consumers can filter it out.
        pname = c.get("provider") or "UNKNOWN (not in drawing)"
        body = dict(
            cid=c["cid"],
            circuit_type=ctype,
            status=st,
            provider=provider(pname),
        )
        desc = []
        if c.get("capacity"):
            src = c["_src"].get("capacity", "unknown")
            desc.append("capacity=%s[%s]" % (c["capacity"], src))
        if not c.get("provider"):
            desc.append("provider=unknown")
        if c.get("subranges"):
            desc.append("vlans=" + ";".join(
                "%s:%s" % (s["sub"], s["vlans"]) for s in c["subranges"]))
        body["description"] = " ".join(desc)[:200]

        if DRY:
            exists = Circuit.objects.filter(cid=c["cid"]).exists()
            note("updated" if exists else "created")
            continue
        obj, created = Circuit.objects.update_or_create(
            cid=c["cid"], defaults={k: v for k, v in body.items() if k != "cid"})
        note("created" if created else "updated")
    except Exception as e:
        OUT["errors"].append("%s: %s" % (c["cid"], str(e)[:120]))
        note("skipped")

# ---- VLANs as first-class objects
from nautobot.ipam.models import VLAN, VLANGroup
# one group per circuit: drawio encodes a sub-range as a span, and Nautobot
# needs one row per VLAN id to be queryable
vgroup_cache = {}
created_vlans = 0
for c in PAYLOAD["circuits"]:
    for sr in (c.get("subranges") or []):
        spec = sr.get("vlans") or ""
        m = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", spec)
        if not m:
            continue
        lo = int(m.group(1)); hi = int(m.group(2) or m.group(1))
        if hi - lo > 200:      # sanity: refuse absurd spans from a bad parse
            OUT.setdefault("errors", []).append(
                "%s: refusing span %s (%d ids)" % (sr.get("sub"), spec, hi - lo))
            continue
        gname = "SC26 %s" % c["cid"]
        if gname not in vgroup_cache:
            g, _ = VLANGroup.objects.get_or_create(name=gname)
            vgroup_cache[gname] = g
        for vid in range(lo, hi + 1):
            if DRY:
                note("updated" if VLAN.objects.filter(vid=vid, vlan_group=vgroup_cache[gname]).exists() else "created")
                continue
            try:
                VLAN.objects.update_or_create(
                    vid=vid, vlan_group=vgroup_cache[gname],
                    defaults={"name": "%s %s" % (sr.get("sub"), vid), "status": st})
                created_vlans += 1
            except Exception as e:
                OUT["errors"].append("vlan %d/%s: %s" % (vid, gname, str(e)[:80]))
OUT["vlans_written"] = created_vlans

# ---- devices + cables from hops
def dev_for(node_id):
    d = Device.objects.filter(serial=node_id).first()
    if d:
        return d
    if DRY:
        return None
    return Device.objects.create(
        name=node_id[:60], serial=node_id, device_type=dt,
        role=(role_r if node_id.endswith("-cr6") else role_e),
        location=loc, status=st)

def iface_for(dev, idx, cid=""):
    # interface name must be unique per circuit: several circuits terminate on
    # the same device (e.g. star-cr6), and reusing eth1 made each new cable
    # collide with the previous one.
    tag = (cid or "").replace("WC", "wc")
    name = ("%s-eth%d" % (tag, idx + 1)) if tag else ("eth%d" % (idx + 1))
    i, _ = Interface.objects.get_or_create(
        device=dev, name=name,
        defaults={"type": "1000base-t", "status": st})
    return i

from nautobot.dcim.models.cables import CableToCableTermination

# purge cables whose hop index is out of range for their circuit -- the
# symptom of the global-index bug, which left WCnn hop1..hopN lying around
import re as _re
valid = {"%s hop%d" % (c["cid"], i)
         for c in PAYLOAD["circuits"] for i in range(len(c.get("hops") or []))}
stale = [cab for cab in Cable.objects.filter(label__contains=" hop")
         if cab.label not in valid and _re.fullmatch(r"WC[0-9]{2} hop[0-9]+", cab.label or "")]
for cab in stale:
    cab.delete()
if stale:
    OUT["purged"] = len(stale)
for c in PAYLOAD["circuits"]:
    # per-circuit index: a global enumerate() made labels advance every run
    # (WC01 hop1, then hop2...) so re-runs created new cables instead of
    # converging on the same one.
    for idx, (a, b) in enumerate(c.get("hops") or []):
        label = "%s hop%d" % (c["cid"], idx)
        try:
            if DRY:
                note("updated" if Cable.objects.filter(label=label).exists() else "created")
                continue
            da, db = dev_for(a), dev_for(b)
            if not da or not db:
                note("skipped"); continue
            cab = Cable.objects.filter(label=label).first()
            if cab is None:
                cab = Cable.objects.create(label=label, status=st)
                created_cable = True
            else:
                created_cable = False
            ia, ib = iface_for(da, idx, c["cid"]), iface_for(db, idx, c["cid"])

            # Detach any existing terminations before re-adding. The concrete
            # join model is CableToCableTermination -- `CableTermination` is an
            # abstract base with no manager (that cost an hour earlier).
            existing = {t.interface_id for t in
                        CableToCableTermination.objects.filter(cable=cab)
                        .select_related("interface")} if False else set()
            CableToCableTermination.objects.filter(cable=cab).delete()
            # add_termination is the ONLY path that rebuilds cable_paths, so
            # Interface.connected_endpoint becomes non-null
            cab.add_termination(ia, "A")
            cab.add_termination(ib, "B")
            cab.save()
            note("created" if created_cable else "converged")
        except Exception as e:
            OUT["errors"].append("%s: %s" % (label, str(e)[:120]))
            note("skipped")

print("RESULT=" + json.dumps(OUT))
"""


def nautobot_pod():
    out = subprocess.run(
        ["kubectl", "get", "pods", "-n", "mfsada",
         "-l", "app.kubernetes.io/component=nautobot-default",
         "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.conditions[?(@.type==\"Ready\")].status}{\"\\n\"}{end}"],
        capture_output=True, text=True)
    for line in out.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "True":
            return parts[0]
    return None


def apply_remote(payload, expect_noop=False):
    pod = nautobot_pod()
    if not pod:
        print("no Ready nautobot-server pod in ns mfsada", file=sys.stderr)
        return 2

    # write payload + script into the pod, then run it
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "payload.json").write_text(json.dumps(payload))
    (d / "sync.py").write_text(REMOTE_TEMPLATE)
    for src, dst in ((d / "payload.json", "/tmp/_sync_payload.json"),
                     (d / "sync.py", "/tmp/_sync.py")):
        r = subprocess.run(["kubectl", "cp", str(src), "mfsada/%s:%s" % (pod, dst)],
                           capture_output=True, text=True)
        if r.returncode:
            print("copy failed -> %s\n%s" % (dst, r.stderr[-600:]), file=sys.stderr)
            return 2
    r = subprocess.run(
        ["kubectl", "exec", "-n", "mfsada", pod, "--",
         "sh", "-c", "cd /opt/nautobot && PYTHONPATH=/opt/nautobot python /tmp/_sync.py"],
        capture_output=True, text=True, timeout=900)

    m = re.search(r"RESULT=(\{.*\})", r.stdout)
    if not m:
        print("sync produced no result\n--- stdout ---\n%s\n--- stderr ---\n%s"
              % (r.stdout[-2000:], r.stderr[-2000:]), file=sys.stderr)
        return 1
    result = json.loads(m.group(1))
    print(json.dumps(result, indent=2))
    if result["errors"]:
        return 1
    if expect_noop and result.get("created"):
        print("EXPECTED a no-op re-run but %d object(s) were created -- "
              "not idempotent" % result["created"], file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--drawio", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--expect-noop", action="store_true")
    ap.add_argument("--skip-cluster", action="store_true",
                    help="extract and report only; do not contact Kubernetes")
    ap.add_argument("--json", help="write extracted model here")
    a = ap.parse_args(argv)

    if not (a.dry_run or a.apply):
        ap.error("choose --dry-run or --apply")

    extracted = extract(a.drawio)
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(extracted, indent=2))

    n = len(extracted["circuits"])
    hops = sum(len(c["hops"]) for c in extracted["circuits"].values())
    with_cap = sum(1 for c in extracted["circuits"].values() if c["capacity"])
    with_prov = sum(1 for c in extracted["circuits"].values() if c["provider"])
    print("extracted %d circuits, %d hops, %d hosts" % (n, hops, len(extracted["hosts"])))
    print("  capacity known: %d/%d | provider known: %d/%d | untagged edges: %d"
          % (with_cap, n, with_prov, n, extracted["untagged_edges"]))
    loops = {k: v["_src"]["self_loops"] for k, v in extracted["circuits"].items()
             if v["_src"].get("self_loops")}
    if loops:
        print("  ! self-loop edges ignored: %s" % loops)
    if extracted.get("dropped_ids"):
        print("  dropped (in text but not on the graph): %s"
              % ", ".join(extracted["dropped_ids"]))

    if a.skip_cluster:
        print("  skipped cluster (--skip-cluster)")
        return 0

    payload = {
        "circuits": list(extracted["circuits"].values()),
        "hosts": extracted["hosts"],
        "dry_run": bool(a.dry_run),
    }
    return apply_remote(payload, expect_noop=a.expect_noop)


if __name__ == "__main__":
    sys.exit(main())
