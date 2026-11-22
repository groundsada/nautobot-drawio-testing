# nautobot-drawio-testing

Proving out one idea: **the hand-drawn drawio stays the authoring surface, and
Nautobot becomes a materialized view of it** — so circuits can be queried
programmatically for health testing without giving up the poster.

```
Tom's drawio  ──tag──▶  <object> metadata  ──extract──▶  model  ──▶  Nautobot
     ▲                                                                  │
     └──────────────── editable in draw.io, styles untouched ◀──────────┘
                                            health scripts query Nautobot
```

## The problem this solves

The drawio is the best way to *see* the network. But circuits living only in a
drawing cannot be pulled programmatically to test their health. Previously the
choice was: keep the beautiful drawing, or get machine-readable circuits.

This repo demonstrates you can have both, because draw.io stores custom
metadata on shapes as `<object>` attributes and renders labels through
`%placeholder%` substitution. The drawing gains a data layer; it does not lose
its styling.

**Proven, not assumed:** tagging changes no rendered pixel. Two PNG renders
(original vs tagged, same input) hash to the same SHA-256:

```
aca1dc13e042dfa67b8030d350c0391f  original
aca1dc13e042dfa67b8030d350c0391f  tagged
```

## Quick start

```bash
# 1. tag a drawio with circuit metadata (adds <object>, touches nothing else)
python3 tools/tag_drawio.py tag diagrams/topology.drawio -o /tmp/tagged.drawio

# 2. prove the tagging was lossless
python3 tools/tag_drawio.py check diagrams/topology.drawio /tmp/tagged.drawio

# 3. see what would sync
python3 tools/sync_nautobot.py --drawio /tmp/tagged.drawio --dry-run

# 4. sync to Nautobot (idempotent)
python3 tools/sync_nautobot.py --drawio /tmp/tagged.drawio --apply
python3 tools/sync_nautobot.py --drawio /tmp/tagged.drawio --apply --expect-noop
```

## Tools

| file | role |
|---|---|
| `tools/tag_drawio.py` | `tag` / `check` / `read` — add metadata, prove losslessness, extract |
| `tools/sync_nautobot.py` | tagged drawio → Nautobot via `nautobot-server shell`, idempotent |
| `rbac/nautobot-pipeline-rbac.yaml` | least-privilege ServiceAccount for the above |

## What the metadata looks like

```xml
<object id="e5" label="WC30" circuit="WC30" circuits="WC30"
        kind="circuit" capacity_inferred="400G"
        source="a1" target="b2">
  <mxCell style="strokeWidth=20;strokeColor=#3399FF;..." edge="1" parent="1">
    <mxGeometry relative="1" as="geometry"/>
  </mxCell>
</object>
```

The `style` attribute is **byte-identical** to the original. Only the wrapper is
new.

## Things learned the hard way (recorded so they are not repeated)

1. **No edge in the real drawing carries a circuit id in its own `value`.**
   All 56 live on *child vertices of edges* (drawio's edge-label form). Tagging
   the label loses `source`/`target` and extracts 0 hops. Tag the **parent
   edge**.
2. **`CableTermination` is abstract** and has no manager. The concrete join
   model is `CableToCableTermination`.
3. **`cable.add_termination()` + `save()`** is the only path that rebuilds
   `cable_paths`, so `Interface.connected_endpoint` becomes non-null.
4. **Cable labels must use a per-circuit hop index.** A global `enumerate()`
   advanced the index on every run, silently creating `hop1`, `hop2`, `hop3`…
   instead of converging.
5. **Nautobot requires a provider.** The drawing has none, so a sentinel
   provider `UNKNOWN (not in drawing)` is used rather than inventing one.
6. **The full 18MB poster exceeds drawio's headless tile memory** when
   exporting to PNG. Use a smaller file to verify rendering.

## RBAC

Namespace-scoped, `mfsada`. Read pods, exec into the Nautobot server pod, read
services. **No** secret access, no pod create/delete, no deployment changes.

`resourceNames` is deliberately not used: it cannot match "the pod with this
label", and the pod name changes on every rollout. The consequence — the SA can
exec into other pods in `mfsada` — is documented in `docs/SECURITY.md` along with
the mitigation (move Nautobot to its own namespace if that becomes a concern).

## Current gaps

- **VLAN sub-ranges are not tagged yet.** They live in separate text boxes, so
  `subranges` extracts empty. The circuit↔VLAN link needs either tagged boxes or
  a label-parse pass.
- **Providers unknown** for all 52 circuits (sentinel used).
- **Capacities are inferred** from stroke width, marked
  `capacity_inferred` rather than `capacity` so consumers can tell the
  difference.
- **WC07 dropped** — appears in drawing text but has no edge, so it is not a
  circuit on the graph.
- Paths are whatever the drawing shows; nothing is fabricated in this repo.
