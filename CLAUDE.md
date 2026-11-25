# Instructions for Claude when working in this repo

This repo is a **drawio -> Nautobot pipeline**. Read this before editing.

## What the pipeline is

```
diagrams/sc26.drawio   (hand-authored, the SOURCE)
        |
        |  tools/tag_drawio.py       wraps shapes in <object> metadata
        |  tools/sync_nautobot.py    extracts a model, writes to Nautobot
        v
Nautobot               (a MATERIALIZED VIEW, never the source)
```

**The drawing is authoritative.** Nautobot is downstream. Never hand-edit
Nautobot to "fix" something — fix the diagram, or the next sync will revert it.

## Non-negotiable invariants

1. **Tagging must be lossless.** `tag_drawio.py check` compares every style,
   geometry, and topology attribute between input and output. If it reports a
   difference, the change is wrong, even if the diagram still renders.
2. **`sync` must be idempotent.** A second run must report `created: 0`. CI
   enforces this with `--expect-noop`.
3. **Never invent a value.** If the drawing does not say a provider or a
   capacity, the answer is `None` or a sentinel, never a plausible guess. A
   fabricated `2400G` shipped once and survived several reviews because nothing
   marked it as invented.
4. **Destructive operations need a gate.** Pruning more than 10% of managed
   rows requires `--allow-bulk-prune`. A demo sync once deleted 49 of 52 real
   circuits.

## How to add a new kind of thing (host, circuit, sub-range)

1. Add a sibling shape in the drawing with `<object kind="...">`.
2. Add extraction in `sync_nautobot.py:extract()`.
3. Add remote creation in `REMOTE_TEMPLATE`.
4. Add a test in `tools/test_pipeline.py`, then **mutation-test it**: break the
   code deliberately and confirm the test fails. A test that cannot fail is
   worse than no test.

## The /draw agent (drawing and Nautobot in one run)

`/draw <change>` on a PR runs `.github/workflows/agent-edit.yml`:
model plan (`tools/llm_edit.py`, ops in `tools/llm_edit_prompt.md`) ->
validated apply to the drawing -> tests, lossless check, drawio-skill lint
(`tools/vendor/drawio_skill/validate.py`) -> render -> commit -> sync the
COMMITTED drawing to Nautobot -> second sync must be a no-op -> reply with the
render and a Mermaid diagram built from Nautobot's read-back
(`tools/nautobot_view.py`).

- The model never writes XML or styles. New capabilities are new ops in the
  `Edit` class, each validating its references, plus a line in the prompt.
- Objects are keyed in Nautobot by the custom field `drawio_id` (the shape
  id), so a rename in the drawing renames the row.
- Ports are named in `extract_inventory()` (`to <peer>`, `#n` in a bundle, or
  explicit `a_ports`/`b_ports`), deterministically, so re-runs converge.
  Drawn ports (`kind="port"`, child cells of their device, from `add_ports`)
  are interfaces too; a link into a device with drawn ports claims free ones
  (`Edit.claim_ports`), so cables land on real port names.
- After all ops, `Edit.run` settles the layout: grown blocks push neighbours
  (and the legend) aside, then every line whose end moved is re-routed.
- Logos are vendored in `logos/` (`tools/fetch_logos.py`, simple-icons CC0);
  an unknown brand draws a text badge, never a guessed mark.
- `/draw undo` restores the drawing from before the last agent commit, as a
  new commit (repeat it to go further back).
- Nautobot access: secret `KUBECONFIG_B64` (service account
  `drawio-pipeline`, see `rbac/`), enabled by variable
  `NAUTOBOT_SYNC_ENABLED=true`.

## Commands

```bash
python3 tools/test_pipeline.py                                    # 53 tests
python3 tools/tag_drawio.py tag diagrams/sc26.drawio -o /tmp/t.drawio
python3 tools/tag_drawio.py check diagrams/sc26.drawio /tmp/t.drawio
python3 tools/sync_nautobot.py --drawio /tmp/t.drawio --dry-run --skip-cluster
```

## Style

- Comments explain *why*, especially for anything that looks odd. Several
  `# NOTE:` comments record a bug that was actually hit.
- No silent fallbacks. If data is missing, report it.
- Prefer failing loudly over producing plausible-but-wrong output.
