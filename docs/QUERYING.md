# Querying the materialized view

Three access paths, all verified against the live cluster.

## 1. REST — simplest, works through a port-forward

```bash
kubectl port-forward -n mfsada svc/nautobot-default 8000:80
curl -H "Authorization: Token $TOKEN" \
  "http://localhost:8000/api/ipam/vlans/?vid=2415"
```

Verified: `count: 1`, `vid 2415`, `name "WC30-2 2415"`.

## 2. ORM inside the pod — fastest, no network hop

```python
from nautobot.ipam.models import VLAN
v = VLAN.objects.filter(vid=2415).select_related("vlan_group").first()
# -> WC30-2 2415, group "SC26 WC30"
```

This is what the sync itself uses. Reachable via
`kubectl exec … nautobot-server shell`.

## 3. GraphQL — best for joins, but mind the cold start

```graphql
{ vlans(vid: 2415) { vid name } }
# -> [{'vid': 2415, 'name': 'WC30-2 2415'}]
```

**Measured cold-start cost: 148 seconds** to build the schema on the first
query after a worker starts. Two consequences:

- A plain `kubectl port-forward` will **drop the connection at ~61s** before the
  first GraphQL query ever returns. This is the transport, not Nautobot.
- Warm up the schema after any Nautobot restart, or use REST/ORM for
  latency-sensitive monitoring.

## The join a health script actually wants

```python
vlan = VLAN.objects.get(vid=2415)
circuit = Circuit.objects.get(cid=vlan.vlan_group.name.replace("SC26 ", ""))
# vlan -> circuit -> description carries capacity + provenance
```

Provenance is in the circuit description, so a consumer can tell a measured
capacity from an inferred one:

```
capacity=400G[inferred-from-stroke] provider=unknown vlans=WC30-2:2410-2419;WC30-1:2400-2409
```

`provider=unknown` is deliberate — the drawing does not carry providers, and a
sentinel is used rather than inventing one.
