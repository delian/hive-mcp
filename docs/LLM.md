# An optional language model

The server needs no model to work. Two curation jobs can use one when you configure it, and both have a useful
fallback without it.

| tool | what it does with a model | without one |
|---|---|---|
| `kb_merge_propose(ids)` | drafts ONE note that merges 2 to 8 notes; **nothing is saved**, you review and call `kb_merge` | refused (write the merge yourself) |
| `kb_conflicts(id)` | asks the model whether each very similar note contradicts this one | heuristics only: near-identical notes whose numbers or yes/no differ |

## Configure
```bash
HIVE_LLM_PROVIDER=anthropic          # or openai (any OpenAI-compatible chat endpoint, also Azure OpenAI)
HIVE_LLM_MODEL=<model id>
HIVE_LLM_API_KEY=...
# openai only: HIVE_LLM_URL=https://.../v1/chat/completions   (Azure: also HIVE_LLM_AZURE=true)
HIVE_LLM_MIN_SCOPE=project           # notes narrower than this are never sent (default: private ones stay home)
```

## What is sent, and what is trusted
- Only notes the asker can read, and only at `HIVE_LLM_MIN_SCOPE` or wider. A private note in a merge request is
  refused before anything leaves the server. Set the scope to `private` only for a model you host yourself.
- Notes go into the prompt between `<note>` tags with an instruction to treat them as data. Prompt injection inside a note
  can still sway the model, so its answer is **only ever a proposal or a hint**: a merge draft passes the usual
  sanitising (secrets masked, hidden characters removed, injection flagged) and is shown, never saved; a conflict
  verdict only reorders and annotates candidates. A failed or garbled model call degrades to the heuristic for conflicts
  and to a clear error for proposals.
- Prompts and answers are not stored by the server. Check your provider's retention terms.

## Ranking nudges (`HIVE_RANK_SIGNALS=true`, off by default)
`kb_rate` votes (one per person and note, changeable) and recent `kb_confirm` confirmations nudge ranking by at most
+/-6% (votes) and +2% (a confirmation in the last 90 days). Relevance decides; the nudges break near-ties. The factors
are counts only, no query text. **They have not been evaluated on real data**: switch them on, then compare
`hive eval` on a set built from your own searches (`hive eval-build`) before keeping them.
