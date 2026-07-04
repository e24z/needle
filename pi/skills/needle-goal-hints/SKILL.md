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
information the agent currently needs. Prefer a task-framed question, not a
keyword list. Good hints often describe one of these needs: locate relevant
code, debug a failure, explain behavior, extend a feature, refactor safely,
verify a fix, compare alternatives, inspect configuration, or find tests.

## Writing good focus questions

State what you want to learn from this specific output, concretely. A good
hint is usually one sentence, often 15-40 words, and readable without the
conversation:

- Complete and self-contained: readable without the conversation. Name the
  symbols, files, errors, or behaviors you are after.
- About your information need, not the command: describe what you want from
  the output, not what the command does.
- Specific over generic: name the function, the config key, the error string.
- Task-framed: say whether you are locating, debugging, explaining, extending,
  verifying, or preparing a safe edit.

**Good:**

- "When debugging the failing setup smoke, which lines identify the command
  that failed, its exit status, and the first actionable error message?"
- "Which definitions and call sites explain how `merge_token_scores_from_chunks`
  combines scores when chunk offsets overlap across adjacent chunks?"
- "For a safe edit to Pi registration, which code paths create, copy, install,
  and later uninstall the Needle package directory?"
- "Which tests or fixtures assert the current behavior of missing focus
  questions, pruning failures, and unchanged pruning decisions?"

**Bad:**

- "What does the output show?" (not a task or information need)
- "The file contents" (not a question; use `verbatim: true` when exact text is needed)
- "Anything relevant" (nothing is specific enough to keep)
- "Fix this" (does not say what evidence to retain)

## What Needle does with it

- Relevant lines are kept; pruned spans collapse to `[pruned]` markers.
- Exit codes and truncation notices are never pruned.
- If pruning fails or the question is missing, the observation arrives
  unpruned with a visible banner — never silently.
- The original text of the last prune is recoverable: the daemon caches it
  per session. Use `/needle original` if a non-idempotent command was
  over-pruned.

If you genuinely need exact output (for example a ranged read before an edit),
set `verbatim: true` on the tool call and still provide a short
`context_focus_question` explaining why exact text is needed. Do this for
patch-sensitive reads, generated lockfile snippets, or other cases where
`[pruned]` markers would make the next edit unsafe. Do not use `verbatim` for
large exploratory reads or noisy command output.
