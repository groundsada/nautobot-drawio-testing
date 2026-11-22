# Security notes

## What the pipeline can do

The `drawio-pipeline` ServiceAccount in `mfsada` can:

- read pods and services in `mfsada`
- `pods/exec` into the Nautobot server pod

It cannot read Secrets, create or delete pods, read deployments, or act outside
`mfsada`. There is no `ClusterRole`.

## The honest caveat about `pods/exec`

`pods/exec` is not a narrow permission. Anyone holding it can run arbitrary
Python inside the Nautobot pod, which already has the database credentials in
its environment. So an authorised exec is effectively "full Nautobot database
access."

That is acceptable here because:

1. The SA is namespace-scoped and bound to a single subject.
2. `mfsada` is already a shared namespace where the human owner holds `*.*`.
   The SA is **strictly less** privileged than the person it serves.
3. CI uses short-lived credentials, minted per job, not a long-lived token in a
   repository secret.

**The alternative considered and rejected:** use the Nautobot REST API with a
scoped API token. That would avoid `pods/exec` entirely. It was rejected on
correctness, not convenience:

- REST create for cables accepts `a_terminations` and **silently ignores it**,
  leaving cables with NULL endpoints. This produced a topology that rendered as
  16 nodes and 0 edges for hours before anyone noticed.
- Bulk upsert measured ~2s per object; 56 circuits took minutes.

If `pods/exec` is unacceptable, the correct fix is to move Nautobot into its own
namespace and keep this Role there, then re-evaluate the REST path with the
termination bug worked around explicitly.

## What is NOT in this repository

- No Nautobot API tokens
- No kubeconfig
- No LLM credentials
- No secrets of any kind

CI authenticates to the cluster using whatever mechanism the runner provides.
Locally, the tools use the operator's own kubeconfig.

## LLM usage

The agent-assisted editing workflow (natural language → drawio edits) uses the
NRP endpoint configured in `~/.config/claude-nrp.env`
(`NRP_BASE_URL`, `NRP_AUTH_TOKEN`). These are read from the environment at run
time and are never written to disk by these tools, logged, or committed.

If this ever runs in CI, the token must come from a CI secret, and the workflow
should be restricted to trusted branches — an agent that can edit diagrams and
trigger a Nautobot sync is an agent that can change the network model.
