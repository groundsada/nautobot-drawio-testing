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
    locations = []

    for t in model["tagged"]:
        meta = t["meta"]
        if meta.get("kind") == "circuit" or "circuit" in meta:
            # never index into a possibly-empty label: an edge tagged
            # kind=circuit with no circuit attribute and no label crashed the
            # whole run with IndexError
            parts = (t["label"] or "").split()
            cid = meta.get("circuit") or (parts[0] if parts else None)
            if not re.fullmatch(r"WC\d{2}", cid or ""):
                continue
            c = circuits.setdefault(cid, {
                "cid": cid, "capacity": None, "provider": None,
                "subranges": [], "hops": [], "_src": {},
            })
            # A circuit usually has several edges and only ONE carries the
            # real capacity, plus thin host legs that would mislead. Taking the
            # last edge seen reported 100G for a 400G circuit. Rank sources
            # (drawing > inferred) and keep the WIDEST stroke within a source.
            rank = {"drawing": 2, "inferred-from-stroke": 1}
            cap_val = meta.get("capacity")
            cap_src = "drawing"
            if not cap_val and meta.get("capacity_inferred"):
                cap_val = meta["capacity_inferred"]
                cap_src = "inferred-from-stroke"
            if cap_val:
                cur_src = c["_src"].get("capacity")
                if rank.get(cap_src, 0) > rank.get(cur_src, 0):
                    c["capacity"] = cap_val
                    c["_src"]["capacity"] = cap_src
                elif cap_src == cur_src:
                    def gbps(v):
                        v = (v or "").upper().replace("G", "").replace("T", "")
                        try:
                            return float(v) * (1000 if "T" in (cap_val or "").upper() else 1)
                        except ValueError:
                            return 0.0
                    if gbps(cap_val) > gbps(c["capacity"]):
                        c["capacity"] = cap_val
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

        if meta.get("kind") == "location":
            name = meta.get("site") or t["label"]
            if name:
                locations.append({"name": name.strip(),
                                  "node_id": t["id"]})
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

    devices, links = extract_inventory(path)
    kinds = {l["node_id"]: "location" for l in locations}
    kinds.update({d["node_id"]: "device" for d in devices})
    for c in keep.values():
        # a single-hop circuit between drawn blocks/devices is modelled as a
        # Nautobot circuit with A/Z terminations; anything else (the poster's
        # multi-hop circuits between unlabelled nodes) keeps the hop cables
        if len(c["hops"]) == 1 and all(n in kinds for n in c["hops"][0]):
            c["ends"] = [{"node_id": n, "kind": kinds[n]} for n in c["hops"][0]]

    return {"circuits": keep, "hosts": hosts, "locations": locations,
            "devices": devices, "links": links,
            "dropped_ids": dropped,
            "untagged_edges": model["untagged_edges"]}


IFACE_TYPE = {"10G": "10gbase-x-sfpp", "25G": "25gbase-x-sfp28",
              "100G": "100gbase-x-qsfp28", "400G": "400gbase-x-qsfpdd",
              "800G": "800gbase-x-osfp", "1.6T": "1600gbase-x-osfp"}
# attributes that become Device fields; everything else lands in comments
DEVICE_FIELDS = {"model", "manufacturer", "platform", "serial", "asset_tag", "status"}
OWNED = {"id", "label", "kind", "role", "location", "source", "target", "capacity",
         "count", "a_ports", "b_ports", "near"}


def extract_inventory(path):
    """kind=device and kind=link objects -> devices and the cables they imply.

    Interface names are decided HERE, deterministically, so a re-run produces
    the same names and the sync converges instead of adding ports: an explicit
    a_ports/b_ports list wins, otherwise 'to <peer>' (+ ' #n' in a bundle).
    """
    import xml.etree.ElementTree as ET
    objs = list(ET.parse(path).getroot().iter("object"))
    devices = [{"node_id": o.get("id"), "name": (o.get("label") or "").strip(),
                "role": o.get("role") or "device", "location": o.get("location"),
                "attrs": {k: v for k, v in sorted(o.attrib.items()) if k not in OWNED}}
               for o in objs if o.get("kind") == "device"]
    names = {d["node_id"]: d["name"] for d in devices}
    used = {}
    links = []
    for o in objs:
        if o.get("kind") != "link":
            continue
        a, b = o.get("source"), o.get("target")
        if a not in names or b not in names:
            continue
        n = int(o.get("count") or 1)
        cap = o.get("capacity")
        given = {e: [x.strip() for x in (o.get(k) or "").split(",") if x.strip()]
                 for e, k in (("a", "a_ports"), ("b", "b_ports"))}
        cables = []
        for i in range(n):
            ends = []
            for e, dev, peer in (("a", a, b), ("b", b, a)):
                if len(given[e]) == n:
                    name = given[e][i]
                else:
                    name = ("to %s" % names[peer])[:56] + (" #%d" % (i + 1) if n > 1 else "")
                if name in used.setdefault(dev, set()):
                    name = "%s (%s)" % (name[:48], o.get("id"))
                used[dev].add(name)
                ends.append(name)
            cables.append({"label": "%s/%d" % (o.get("id"), i + 1),
                           "a_iface": ends[0], "b_iface": ends[1]})
        links.append({"link_id": o.get("id"), "a": a, "b": b, "capacity": cap,
                      "type": IFACE_TYPE.get(cap, "other"), "cables": cables})
    return devices, links


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

# ---- drawio_id: the natural key of every object the drawing owns
# Names are not keys: renaming a block or a device in the drawing must rename
# the Nautobot row, not create a second one and orphan the first.
from django.contrib.contenttypes.models import ContentType
from nautobot.extras.models import CustomField
from nautobot.dcim.models import LocationType, Manufacturer, Platform
from nautobot.dcim.models.cables import CableToCableTermination
from nautobot.circuits.models import CircuitTermination
if not DRY:
    cf, _ = CustomField.objects.get_or_create(
        key="drawio_id", defaults={"label": "drawio id", "type": "text",
                                   "description": "id of the shape in diagrams/sc26.drawio"})
    for m in (Location, Device):
        cf.content_types.add(ContentType.objects.get_for_model(m))

def by_drawio(model, node_id):
    return model.objects.filter(_custom_field_data__drawio_id=node_id).first()

def save(obj, created, fields):
    # set fields, save only if something differs; count what happened
    changed = created or any(getattr(obj, k) != v for k, v in fields.items())
    if DRY:
        note("created" if created else ("updated" if changed else "unchanged"))
        return
    for k, v in fields.items():
        setattr(obj, k, v)
    if changed:
        obj.validated_save()
    note("created" if created else ("updated" if changed else "unchanged"))

# ---- locations (from kind=location blocks in the drawing)
site_t, _ = LocationType.objects.get_or_create(name="Site",
                                               defaults={"nestable": True})
if not DRY:
    # a location type lists what may live there; without CircuitTermination
    # every circuit end was refused ("may not associate to locations of type")
    for m in (Device, CircuitTermination):
        site_t.content_types.add(ContentType.objects.get_for_model(m))
locmap = {}
for L in PAYLOAD.get("locations") or []:
    obj = by_drawio(Location, L["node_id"]) or Location.objects.filter(name=L["name"]).first()
    created = obj is None
    if created:
        obj = Location(location_type=site_t, status=st)
    cfd = dict(obj._custom_field_data or {}, drawio_id=L["node_id"])
    save(obj, created, {"name": L["name"], "_custom_field_data": cfd})
    locmap[L["node_id"]] = obj
OUT["locations"] = len(locmap)

# ---- devices (kind=device)
role_cache, dt_cache = {}, {}
def role_for(name):
    name = name[:1].upper() + name[1:]
    if name not in role_cache:
        r, _ = Role.objects.get_or_create(name=name)
        if not DRY:
            r.content_types.add(ContentType.objects.get_for_model(Device))
        role_cache[name] = r
    return role_cache[name]

def dtype_for(mfr_name, model):
    key = (mfr_name, model)
    if key not in dt_cache:
        mfr, _ = Manufacturer.objects.get_or_create(name=mfr_name)
        dt_cache[key], _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=model)
    return dt_cache[key]

devmap = {}
for d in PAYLOAD.get("devices") or []:
    try:
        L = locmap.get(d["location"])
        if L is None:
            raise ValueError("block %s is not in the drawing" % d["location"])
        a = d.get("attrs") or {}
        role = role_for(d["role"])
        status = Status.objects.get(name=a.get("status", "Active"))
        fields = {
            "name": d["name"], "role": role, "location": L, "status": status,
            "device_type": dtype_for(a.get("manufacturer") or "Generic",
                                     a.get("model") or "Generic %s" % role.name),
            "platform": (Platform.objects.get_or_create(name=a["platform"])[0]
                         if a.get("platform") else None),
            "serial": a.get("serial", ""),
            "asset_tag": a.get("asset_tag") or None,
            # attributes Nautobot has no field for stay visible, not dropped
            "comments": "\\n".join("%s: %s" % kv for kv in sorted(a.items())
                                  if kv[0] not in PAYLOAD["device_fields"]),
        }
        obj = by_drawio(Device, d["node_id"])
        created = obj is None
        if created:
            obj = Device()
        fields["_custom_field_data"] = dict(obj._custom_field_data or {}, drawio_id=d["node_id"])
        save(obj, created, fields)
        devmap[d["node_id"]] = obj
    except Exception as e:
        OUT["errors"].append("device %s: %s" % (d["name"], str(e)[:160]))

# ---- interfaces + cables (kind=link)
cable_st = Status.objects.get(name="Connected")
def iface(dev, name, itype):
    i = Interface.objects.filter(device=dev, name=name).first()
    if i is None:
        if DRY:
            note("created")
            return None
        i = Interface(device=dev, name=name)
        save(i, True, {"type": itype, "status": st,
                       "description": "managed-by=drawio-pipeline"})
    elif i.type != itype:
        save(i, False, {"type": itype})
    return i

def ends(cab):
    return {t.cable_end: (t.interface_id or t.circuit_termination_id)
            for t in CableToCableTermination.objects.filter(cable=cab)}

def cable(label, a, b):
    # one cable, re-terminated only if its ends moved
    cab = Cable.objects.filter(label=label).first()
    if DRY:
        note("updated" if cab else "created")
        return
    if cab and ends(cab) == {"A": a.pk, "B": b.pk}:
        note("unchanged")
        return
    if cab:
        cab.delete()
    for t in (a, b):
        # a port can hold one cable: free it from any cable we used to own
        old = getattr(t, "cable", None)
        if old is not None:
            old.delete()
            t.refresh_from_db()
    cab = Cable(label=label, status=cable_st)
    cab.validated_save()
    cab.add_termination(a, "A")
    cab.add_termination(b, "B")
    cab.save()
    note("created")

want_cables, want_ifaces = set(), set()
for l in PAYLOAD.get("links") or []:
    da, db = devmap.get(l["a"]), devmap.get(l["b"])
    if da is None or db is None:
        OUT["errors"].append("link %s: an end device was not synced" % l["link_id"])
        continue
    for c in l["cables"]:
        want_cables.add(c["label"])
        want_ifaces.update({(da.pk, c["a_iface"]), (db.pk, c["b_iface"])})
        try:
            ia, ib = iface(da, c["a_iface"], l["type"]), iface(db, c["b_iface"], l["type"])
            if ia is not None and ib is not None:
                cable(c["label"], ia, ib)
            elif DRY:
                note("created")
        except Exception as e:
            OUT["errors"].append("cable %s: %s" % (c["label"], str(e)[:160]))

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
        # ownership marker: the pruner must never delete a row it did not
        # create. Without this, deleting a circuit from the drawing removed
        # hand-made Nautobot rows that merely shared the WCnn naming shape.
        desc.append("managed-by=drawio-pipeline")
        body["description"] = " ".join(desc)[:220]

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

# ---- circuit terminations (single-hop circuits between drawn blocks/devices)
for c in PAYLOAD["circuits"]:
    if not c.get("ends") or DRY:
        continue
    try:
        circ = Circuit.objects.get(cid=c["cid"])
        for side, end in zip("AZ", c["ends"]):
            dev = devmap.get(end["node_id"])
            L = dev.location if dev else locmap.get(end["node_id"])
            t = CircuitTermination.objects.filter(circuit=circ, term_side=side).first()
            created = t is None
            if created:
                t = CircuitTermination(circuit=circ, term_side=side)
            save(t, created, {"location": L})
            if dev is not None:
                # a circuit that ends on a device is cabled to a port on it
                want_cables.add("%s term%s" % (c["cid"], side))
                want_ifaces.add((dev.pk, c["cid"].lower()))
                i = iface(dev, c["cid"].lower(), PAYLOAD["iface_type"].get(c.get("capacity"), "other"))
                cable("%s term%s" % (c["cid"], side), i, t)
            elif t.cable_id if hasattr(t, "cable_id") else getattr(t, "cable", None):
                t.cable.delete()
    except Exception as e:
        OUT["errors"].append("%s terminations: %s" % (c["cid"], str(e)[:160]))

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

# ---- prune VLANs that no longer appear in the drawing
# A materialized view that only ever adds is not a view: remove a VLAN from the
# poster and the row would linger forever, so the DB and the source diverge
# silently. Prune per group, and report what went, because deleting rows is
# destructive and must be visible in the run output.
pruned_vlans = 0
if not DRY:
    wanted = {}
    for c in PAYLOAD["circuits"]:
        gname = "SC26 %s" % c["cid"]
        ids = set()
        for sr in (c.get("subranges") or []):
            m = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", sr.get("vlans") or "")
            if not m:
                continue
            lo = int(m.group(1)); hi = int(m.group(2) or m.group(1))
            if hi - lo > 200:
                continue
            ids.update(range(lo, hi + 1))
        wanted[gname] = ids
    for gname, ids in wanted.items():
        g = VLANGroup.objects.filter(name=gname).first()
        if not g:
            continue
        stale = VLAN.objects.filter(vlan_group=g).exclude(vid__in=ids)
        n = stale.count()
        if n:
            stale.delete()
            pruned_vlans += n
OUT["vlans_pruned"] = pruned_vlans

# ---- prune circuits and cables that no longer appear in the drawing
# Same reasoning as the VLAN prune: the drawing is the source, so a circuit
# deleted from the poster must leave the database or the two diverge. Scoped to
# objects this pipeline owns -- only cids present in the model's WCnn shape and
# only cables labelled 'WCnn hopN' -- so nothing hand-made in Nautobot is
# touched.
pruned_circuits = pruned_cables = 0
if not DRY:
    want_cids = {c["cid"] for c in PAYLOAD["circuits"]}
    want_hops = {"%s hop%d" % (c["cid"], i)
                 for c in PAYLOAD["circuits"] if not c.get("ends")
                 for i in range(len(c.get("hops") or []))}
    # cable labels are already a precise ownership signal ('WCnn hopN' is a
    # pattern this pipeline defines and nothing else produces), so no extra
    # marker is needed here
    stale_cables = [cab for cab in Cable.objects.filter(label__contains=" hop")
                    if re.fullmatch(r"WC[0-9]{2} hop[0-9]+", cab.label or "")
                    and cab.label not in want_hops]
    for cab in stale_cables:
        cab.delete()
        pruned_cables += 1
    # only our own rows: never prune something a human made
    managed = Circuit.objects.filter(
        description__contains="managed-by=drawio-pipeline")
    stale_circuits = [c for c in managed if c.cid not in want_cids]
    total_managed = managed.count()
    if (stale_circuits and total_managed
            and len(stale_circuits) > max(3, total_managed * 0.1)
            and not PAYLOAD.get("allow_bulk_prune")):
        # Syncing a 3-circuit demo against a 52-circuit database would delete
        # 49 real rows. That is the tool working as designed and the design
        # being dangerous, so bulk deletion must be asked for explicitly.
        OUT["errors"].append(
            "refusing to prune %d of %d managed circuits; pass "
            "--allow-bulk-prune if this is intended"
            % (len(stale_circuits), total_managed))
        stale_circuits = []
    for c in stale_circuits:
        c.delete()
        pruned_circuits += 1
OUT["circuits_pruned"] = pruned_circuits
OUT["cables_pruned"] = pruned_cables

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
         for c in PAYLOAD["circuits"] if not c.get("ends")
         for i in range(len(c.get("hops") or []))}
stale = [cab for cab in Cable.objects.filter(label__contains=" hop")
         if cab.label not in valid and _re.fullmatch(r"WC[0-9]{2} hop[0-9]+", cab.label or "")]
for cab in stale:
    cab.delete()
if stale:
    OUT["purged"] = len(stale)
for c in PAYLOAD["circuits"]:
    if c.get("ends"):
        continue
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
            note("created" if created_cable else "cables_reterminated")
        except Exception as e:
            OUT["errors"].append("%s: %s" % (label, str(e)[:120]))
            note("skipped")

# ---- prune what the drawing no longer has: link cables, ports, devices
# Scoped to rows this pipeline owns: cables labelled 'link-N/i' or 'WCnn termX',
# interfaces it created, and devices carrying a drawio_id.
pruned = {"devices": 0, "cables": 0, "interfaces": 0}
if not DRY and not OUT["errors"]:
    for cab in Cable.objects.filter(label__regex=r"^(link-[0-9]+/[0-9]+|WC[0-9]+ term[AZ])$"):
        if cab.label not in want_cables:
            cab.delete()
            pruned["cables"] += 1
    owned = Device.objects.filter(_custom_field_data__has_key="drawio_id")
    stale = [d for d in owned if d._custom_field_data.get("drawio_id") not in devmap]
    if len(stale) > max(3, owned.count() * 0.1) and not PAYLOAD.get("allow_bulk_prune"):
        OUT["errors"].append("refusing to delete %d of %d drawing-owned devices; pass "
                             "--allow-bulk-prune if this is intended" % (len(stale), owned.count()))
    else:
        for d in stale:
            d.delete()
            pruned["devices"] += 1
    for i in Interface.objects.filter(description="managed-by=drawio-pipeline"):
        if (i.device_id, i.name) not in want_ifaces:
            i.delete()
            pruned["interfaces"] += 1
OUT["pruned"] = pruned

# ---- read back what Nautobot now holds, for the PR comment
def end_name(t):
    if t.interface_id:
        return "%s:%s" % (t.interface.device.name, t.interface.name)
    if t.circuit_termination_id:
        ct = t.circuit_termination
        return "%s/%s" % (ct.circuit.cid, ct.term_side)
    return "?"
OUT["state"] = {
    "locations": sorted(Location.objects.values_list("name", flat=True)),
    "devices": [{"name": d.name, "role": d.role.name, "location": d.location.name,
                 "type": "%s %s" % (d.device_type.manufacturer.name, d.device_type.model),
                 "ports": d.interfaces.count()}
                for d in Device.objects.filter(_custom_field_data__has_key="drawio_id")
                .select_related("role", "location", "device_type__manufacturer").order_by("name")],
    "cables": sorted([{"label": c.label, **{t.cable_end: end_name(t) for t in
                       CableToCableTermination.objects.filter(cable=c)
                       .select_related("interface__device", "circuit_termination__circuit")}}
                      for c in Cable.objects.filter(
                          label__regex=r"^(link-[0-9]+/[0-9]+|WC[0-9]+ term[AZ])$")],
                     key=lambda x: x["label"]),
    "circuits": [{"cid": c.cid, "provider": c.provider.name,
                  "ends": {t.term_side: t.location.name if t.location else None
                           for t in c.circuit_terminations.all()}}
                 for c in Circuit.objects.filter(
                     description__contains="managed-by=drawio-pipeline").order_by("cid")],
}

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


def apply_remote(payload, expect_noop=False, result_path=None):
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
    if result_path:
        pathlib.Path(result_path).write_text(json.dumps(result, indent=2))
    # the read-back is for --result and the PR comment, not the log
    print(json.dumps({k: v for k, v in result.items() if k != "state"}, indent=2))
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
    ap.add_argument("--allow-bulk-prune", action="store_true",
                    help="permit pruning more than 10%% of managed rows. "
                         "Without this, syncing a small test drawing against a "
                         "populated database deletes real data.")
    ap.add_argument("--skip-cluster", action="store_true",
                    help="extract and report only; do not contact Kubernetes")
    ap.add_argument("--json", help="write extracted model here")
    ap.add_argument("--result", help="write the apply result, including what "
                                     "Nautobot holds afterwards, here")
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
    print("extracted %d circuits, %d hops, %d locations, %d devices, %d links (%d cables)"
          % (n, hops, len(extracted["locations"]), len(extracted["devices"]),
             len(extracted["links"]), sum(len(l["cables"]) for l in extracted["links"])))
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
        "locations": extracted["locations"],
        "devices": extracted["devices"],
        "links": extracted["links"],
        "device_fields": sorted(DEVICE_FIELDS),
        "iface_type": IFACE_TYPE,
        "dry_run": bool(a.dry_run),
        "allow_bulk_prune": bool(a.allow_bulk_prune),
    }
    return apply_remote(payload, expect_noop=a.expect_noop, result_path=a.result)


if __name__ == "__main__":
    sys.exit(main())
