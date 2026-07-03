---
name: needle-goal-hints
description: How to write the required context_focus_question parameter on read and bash tool calls when the Needle pruning extension is active. Use whenever calling read or bash in a Needle-enabled session, or when tool output comes back with a "missing context_focus_question" banner.
---

# Needle Goal Hints

Needle prunes `read` and `bash` observations down to the lines relevant to your
current information need, using a local model. The `context_focus_question`
parameter is how you tell it what you need. Pruning quality is bounded by the
quality of that question.

This follows SWE-Pruner's goal-hint design (Section 3.2, "Goal Hint
Generation"): the hint is a complete, self-contained question describing the
information the agent currently needs.

## Writing good focus questions

State what you want to learn from this specific output, concretely:

- Complete and self-contained: readable without the conversation. Name the
  symbols, files, errors, or behaviors you are after.
- About your information need, not the command: describe what you want from
  the output, not what the command does.
- Specific over generic: name the function, the config key, the error string.

**Good:**

- "How does `merge_token_scores_from_chunks` combine scores when chunk offsets overlap?"
- "Which test failed and what was the assertion error?"
- "Where is the retry limit for HTTP requests configured, and what is its default?"

**Bad:**

- "What does the output show?" (not an information need)
- "The file contents" (not a question)
- "Anything relevant" (nothing is specific enough to keep)

## What Needle does with it

- Relevant lines are kept; pruned spans collapse to `[pruned]` markers.
- Exit codes and truncation notices are never pruned.
- If pruning fails or the question is missing, the observation arrives
  unpruned with a visible banner — never silently.
- The original text of the last prune is recoverable: the daemon caches it
  per session (useful when a non-idempotent command was over-pruned).

If you genuinely need an entire file verbatim (e.g. before an edit), say so:
"The complete contents of this file, verbatim, to prepare an edit." Needle's
floor keeps structure, and small outputs are never pruned.
