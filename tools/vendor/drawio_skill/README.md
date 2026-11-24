# drawio-skill (vendored)

`validate.py` is copied unchanged from
[Agents365-ai/drawio-skill](https://github.com/Agents365-ai/drawio-skill)
(`skills/drawio-skill/scripts/validate.py`, commit `9dd28000c6a3`, MIT, see
`LICENSE`). It is a stdlib-only structural linter for .drawio files: dangling
edges, duplicate ids, broken parents, overlapping shapes, and edges whose
waypoints run through a shape or cross another edge.

The agent workflow runs it on every `/draw` result and posts its findings.
Errors fail the edit; warnings are reported. Upgrade by copying a newer
`validate.py` and re-running `python3 tools/test_pipeline.py`.
