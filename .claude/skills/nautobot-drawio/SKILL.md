---
name: nautobot-drawio
description: Edit the SC26 drawio topology and push changes to Nautobot. Use when asked to add/remove circuits, hosts, sub-ranges, or locations in this repo, or to sync the diagram to Nautobot.
---

# Editing the SC26 topology

The drawing at `diagrams/sc26.drawio` is the source of truth. Nautobot is
generated from it.

## Before any edit

```bash
python3 tools/test_pipeline.py     # must be 30/30 before you start
```

If tests fail, fix that first. Do not edit the diagram on a red baseline.

## Adding a thing

Everything is an `<object>` wrapper around an `<mxCell>`. The `kind` attribute
is what the sync reads.

| kind | creates | required attrs |
|---|---|---|
| `location` | a Nautobot Location | `site` |
| `circuit` | a Nautobot Circuit | `circuit` |
| `subrange` | ipam.VLAN rows | `circuits`, `subs` |
| `host` | (planned) | `hostname`, `circuit` |

**Copy a sibling's `style` attribute verbatim.** Never author a style string —
the point of this pipeline is that the drawing's visual conventions are
preserved, and an invented style breaks that.

## After any edit

```bash
python3 tools/tag_drawio.py tag diagrams/sc26.drawio -o /tmp/t.drawio
python3 tools/tag_drawio.py check diagrams/sc26.drawio /tmp/t.drawio   # MUST say OK
python3 tools/sync_nautobot.py --drawio /tmp/t.drawio --dry-run --skip-cluster
```

Compare the extracted model to what you intended. Then let the PR workflow post
the plan; do not run `--apply` locally unless asked.

## Things that will bite

- **Deleting a shape deletes the Nautobot row** on the next sync. Confirm
  before removing anything.
- **A circuit id in prose is not a circuit.** WC32 appears in text but has no
  edge; extraction drops it with a reason.
- **Capacity comes from stroke width**, and the real file maps 400G to widths
  20, 40 *and* 60 — so an unseen width returns `None`. Do not "fix" that by
  guessing.
- **The tagged XML must stay valid.** Escape `<`, `>`, `&` in attribute values
  (`&lt;br&gt;`, not `<br>`). drawio renders invalid XML anyway; the tagger
  will not.
