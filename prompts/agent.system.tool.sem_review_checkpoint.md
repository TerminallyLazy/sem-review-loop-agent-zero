# sem_review_checkpoint

Record the final project-scoped Semantic Review result after the response is
complete. This is a local review gate, not a general progress note.

Call this tool exactly once before reporting completion of a coding task when
the Semantic Review service is enabled and a current working-diff fingerprint
is available. Use the exact fingerprint from the latest working review. Include
every structural entity ID from that snapshot exactly once, plus short
source-free findings. Choose `pass` when the review found no unresolved issue,
`repaired` when the bounded repair loop fixed findings, `unresolved` when a
finding remains, or `cancelled` when the user stopped the review.

```json
{
  "tool_name": "sem_review_checkpoint",
  "fingerprint": "<exact current working fingerprint>",
  "outcome": "pass | repaired | unresolved | cancelled",
  "structural_entities": ["<exact structural entity ID>"],
  "findings": ["<short source-free finding>"]
}
```

Do not invent a fingerprint or entity ID. If the checkpoint is rejected because
the working tree changed, refresh the working diff and retry with the new
fingerprint. Never include source code, secrets, or file contents in findings.
