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

## Ranking nudge (`HIVE_RANK_SIGNALS=true`, off by default)
A note a person confirmed with `kb_confirm` in the last 90 days is ranked +2% higher. Relevance decides; the nudge breaks
near-ties and cannot lift a clearly worse match. `kb_rate` votes used to nudge ranking too (+/-6%) and no longer do: a
vote belongs to a note, not to a question, so a note that is right for one question and noise for another collected both
kinds, and a replay with simulated voters showed no gain at any voter accuracy (MRR 0.700 without, about 0.68 with).
Ratings remain feedback data (`HIVE_SEARCH_FEEDBACK`, `hive eval-build`).

## Measuring them: `hive eval-assist`
```bash
hive eval-assist conflicts evals/conflicts.json   # heuristic; with HIVE_LLM_* also the model, and the pipeline as shipped
hive eval-assist merges evals/merges.json         # needs a model: how many required facts survive in the draft
```
What the shipped sets say (written by the developers; they check the machinery, not your notes):
- **Contradictions, heuristic only** (24 pairs, 12 real contradictions): precision 0.57, recall 0.67. It finds changed
  numbers and added "not"/"never", misses contradictions with neither (Tuesday vs Thursday, enabled vs disabled, another
  owner), and cries wolf on unrelated numbers or a "do not" about something else. That is why a model, when configured,
  gets the last word, and why the heuristic only reorders candidates and never blocks anything.
- **A model, measured once**: no API key was available, so the exact prompts of the 24 pairs and 3 merge groups were
  answered by a Claude model (Sonnet, through a subagent, not through the server's HTTP provider) and scored by the same
  code. Contradictions: precision 1.00, recall 1.00 (12/12 found, 0 false alarms), against 0.57/0.67 for the heuristic.
  Merges: every required fact kept in all 3 drafts, and the group with two different cache TTLs was reported as a
  disagreement instead of picking one. The set is small and easy, so read this as "the prompts and the scoring work",
  not as an accuracy figure; subtler contradictions and long notes need your own set. Run the command with your provider
  to measure the provider itself; the row to read is the pipeline as shipped (model, else heuristic).

Write your own `conflicts.json` (`{"pairs": [{"a": {title, body}, "b": {...}, "contradict": true}]}`) and
`merges.json` (`{"groups": [{"notes": [{title, body}], "facts": ["must appear"]}]}`) from real notes.
