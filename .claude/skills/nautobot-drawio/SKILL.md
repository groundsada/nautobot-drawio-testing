---
name: nautobot-drawio
description: Edit the SC26 drawio topology and push changes to Nautobot. Use when asked to add, change, move or remove blocks, devices, links, circuits or sub-ranges in this repo, or to sync the diagram to Nautobot.
---

# Editing the SC26 topology

The drawing at `diagrams/sc26.drawio` is the source of truth. Nautobot is
generated from it.

## Before any edit

```bash
python3 tools/test_pipeline.py     # must be all green before you start
```

If tests fail, fix that first. Do not edit the diagram on a red baseline.

## Adding a thing

Everything is an `<object>` wrapper around an `<mxCell>`. The `kind` attribute
is what the sync reads.

| kind | creates | required attrs |
|---|---|---|
| `location` | a Nautobot Location | `site` |
| `device` | a Device (+ role, type, platform) | `role`, `location` (block id) |
| `link` | one Cable + two Interfaces per `count` | `source`, `target` (devices), `capacity` |
| `circuit` | a Circuit with A/Z terminations | `circuit`, `capacity` |
| `subrange` | ipam.VLAN rows | `circuits`, `subs` |
| `note` | nothing (annotation only) | |

**Prefer the edit tool over hand-written XML.** Write a plan and apply it:

```bash
python3 tools/llm_edit.py --drawio diagrams/sc26.drawio --instruction x \
  --plan plan.json -o diagrams/sc26.drawio
```

The ops are listed in `tools/llm_edit_prompt.md`. The tool draws in the file's
conventions, lays devices out, routes lines around blocks, refuses a plan with
any bad reference, and proves every untouched shape is byte-identical. If you
must hand-edit, copy a sibling's `style` verbatim; never invent one. Lint with
`python3 tools/vendor/drawio_skill/validate.py diagrams/sc26.drawio --score`
(vendored from Agents365-ai/drawio-skill).

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
