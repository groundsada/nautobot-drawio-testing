# How to test this

Four levels, fast to slow. Each one you can verify yourself.

## Level 0 — see it rendered

```
open demo/topology.demo.png
```

A 3-circuit drawing showing **thick blue 400G** vs **thin black 100G**, grouped
boxes, and a host shape. This is what tagging must not change.

## Level 1 — 30 seconds, no cluster

```bash
python3 tools/test_pipeline.py            # 29 assertions
```

Every test maps to a bug that actually shipped. Mutation-verified: break the
code and the relevant test goes red.

Then the pipeline on the demo:

```bash
python3 tools/tag_drawio.py tag demo/topology.demo.drawio -o /tmp/t.drawio
#   wrapped 5 edges (5 labels), already 0
#   wrapped 2 boxes carrying 3 sub-ranges (already 0)

python3 tools/tag_drawio.py check demo/topology.demo.drawio /tmp/t.drawio
#   OK: every style, geometry, and topology attribute is unchanged

python3 tools/sync_nautobot.py --drawio /tmp/t.drawio --dry-run --skip-cluster \
        --json /tmp/model.json
#   extracted 3 circuits, 5 hops
```

Expected in `/tmp/model.json`: `WC30 capacity 400G`, `WC10 capacity 100G`.
If those swap, the stroke mapping is broken.

## Level 2 — prove tagging changes nothing visually

```bash
cd demo
drawio --no-sandbox --export --format png --scale 2 topology.demo.drawio
cd ..
python3 tools/tag_drawio.py tag demo/topology.demo.drawio -o /tmp/t.drawio
cp /tmp/t.drawio demo/tagged-only.drawio
cd demo && drawio --no-sandbox --export --format png --scale 2 tagged-only.drawio
cd ..
shasum demo/topology.demo.png demo/tagged-only.png
```

**The two hashes must be identical.** That is the central claim.

## Level 3 — against the live cluster

```bash
kubectl port-forward -n mfsada svc/nautobot-default 8000:80   # in one shell
```

Then, from another:

```bash
# 1. read-only preview -- shows what WOULD change
python3 tools/sync_nautobot.py --drawio /tmp/t.drawio --dry-run

# 2. apply
python3 tools/sync_nautobot.py --drawio /tmp/t.drawio --apply

# 3. apply AGAIN -- must report created: 0
python3 tools/sync_nautobot.py --drawio /tmp/t.drawio --apply --expect-noop
```

Step 3 is the idempotency proof. `--expect-noop` exits non-zero if anything is
created.

**Careful:** syncing a *small* drawing prunes circuits missing from it. The tool
refuses if it would remove more than 10% of managed rows:

```
refusing to prune 49 of 52 managed circuits; pass --allow-bulk-prune if this is intended
```

Do not pass that flag against a populated database unless you mean it.

## Level 4 — query the result

```bash
TOKEN=<your token>
curl -H "Authorization: Token $TOKEN" \
  "http://localhost:8000/api/ipam/vlans/?vid=2415"
# -> count 1, name "WC30-2 2415"
```

Or inside the pod (faster, no port-forward needed):

```bash
kubectl exec -n mfsada deploy/nautobot-default -c nautobot -- \
  nautobot-server shell -c "
from nautobot.ipam.models import VLAN
print(VLAN.objects.filter(vid=2415).values('vid','name','vlan_group__name'))"
```

## What "passing" looks like

| check | expected |
|---|---|
| `test_pipeline.py` | 29/29 |
| `check` after tagging | no problems |
| demo PNG hashes | identical |
| extract on demo | WC30 400G, WC10 100G |
| second `--apply` | `created: 0` |
| VLAN 2415 query | `WC30-2 2415` |

## Known things that are NOT bugs

- **`provider=unknown`** — the drawing has no providers; a sentinel is used on
  purpose rather than inventing one.
- **`capacity[inferred-from-stroke]`** — derived from line width, not a label.
  The drawing's widths are not injective (400G at 20, 40 *and* 60 px), so this
  is evidence, not measurement.
- **WC07 dropped** — it has an edge with a source but no target in the drawing.
- **`demo/` renders; `diagrams/topology.example.drawio` does not** — the latter
  exceeds drawio's headless tile memory.

## If you want to test against Tom's real file

```bash
python3 tools/tag_drawio.py tag ~/Downloads/sc26-demonstration-topology.drawio.xml \
        -o /tmp/real.drawio
python3 tools/tag_drawio.py check ~/Downloads/sc26-demonstration-topology.drawio.xml \
        /tmp/real.drawio
python3 tools/sync_nautobot.py --drawio /tmp/real.drawio --dry-run --skip-cluster
#   extracted 52 circuits, 55 hops, 63 sub-ranges
```

Tagging the real 18MB file takes ~5s. Rendering it to PNG will not work — that
is drawio's limit, not ours.
