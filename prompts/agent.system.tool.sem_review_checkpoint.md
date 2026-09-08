# sem_review_checkpoint

Inspect the current review state or record its final project-scoped result
before calling the final response tool. This is a local review gate, not a general progress note.

Record a checkpoint before reporting completion of a coding task when
the Semantic Review service is enabled and a current working-diff fingerprint
is available. Use the exact fingerprint from the latest working review. Include
every structural entity ID from that snapshot exactly once, plus short
source-free findings. Choose `pass` when the review found no unresolved issue,
`repaired` when the bounded repair loop fixed findings, `unresolved` when a
finding remains, or `cancelled` when the user stopped the review.

First call with `action: "status"` to refresh and obtain the exact current
fingerprint and structural entity IDs. The native sem MCP tools return code
analysis, not this plugin's checkpoint fingerprint. Do not search their outputs
for it or guess it. Status does not record a checkpoint or change source.

```json
{
  "tool_name": "sem_review_checkpoint",
  "tool_args": {"action": "status"}
}
```

After reviewing those entities, record the result (default action is `record`):

```json
{
  "tool_name": "sem_review_checkpoint",
  "tool_args": {
    "fingerprint": "<exact current working fingerprint>",
    "outcome": "pass",
    "structural_entities": ["<exact structural entity ID>"],
    "findings": []
  }
}
```

Do not invent a fingerprint or entity ID. If the checkpoint is rejected because
the working tree changed, refresh the working diff and retry with the new
fingerprint. Never include source code, secrets, or file contents in findings.

Only propose a lesson when this review taught something specific and reusable.
Optionally include `lesson` in `tool_args`, with exactly `problem` and
`resolution` (short, source-free prose). Describe the actual defect and the
verified fix; omit it for routine passes. Lessons are proposed only for `pass`
or `repaired` outcomes and need user approval before future use.
