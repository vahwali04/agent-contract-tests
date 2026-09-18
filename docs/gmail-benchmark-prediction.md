# Pre-registered prediction: the inbox-cleanup benchmark

> **The inputs are not in this repository.** The agent prompt, its mock tool
> server and the ten contracts were written by another team and shared for
> this benchmark; publishing their work is not this repo's call to make. The
> commands below name paths that are absent here, and the result is recorded
> without the material it was measured on. Numbers stated here are therefore
> not independently reproducible from this repository alone — they are a
> record of what was run, not an artefact you can re-run.

Written **before** seeing the contracts, so the result cannot be rationalised
after the fact. This project has twice published a number that turned out to
measure something other than what it claimed; writing the expectation down
first is the cheapest guard against a third.

Date: 2026-09-17
Extractor at: `docs/gmail-benchmark-prediction.md` added in this commit

## Why this benchmark

Banking and support were both authored here. A 9/9 on a domain whose
contracts, prompt and schema all originate from the same hand is not
evidence of generalisation, and has already been caught measuring exactly
that once — the extraction prompt's worked examples were the support suite's
own rules.

The inbox-cleanup agent is the first case where all three inputs come from
someone else: a different agent shape, a prompt at a known hash, and ten
contracts written by a team that did not build the extractor.

## What is needed to run it

Not currently in this repository. `.github/workflows/inbox-cleanup-no-tunnel.yml`
references `external/inbox-cleanup-agent/` and
`external/inbox-cleanup-contracts-scoped/`, neither of which has ever been
tracked in git — that workflow cannot have run from a clean checkout.

1. The ten contracts (`*.yaml`)
2. `inbox-cleanup.md` — the system prompt, sha256 `5ddd7dabf494…`
3. The tool schema, as a `GET /tools` response or a JSON file

## The claim being tested

That the banking result (9/9 recall, 0 invented, 0 wrong fields) reflects the
extractor rather than the author.

## Prediction

The working hypothesis offered was *six mechanical invariants reachable from
schemas, four adversarial ones reachable only from prose*. A schema-only run
against a representative Gmail-shaped vocabulary suggests the first half of
that split is optimistic, and the reason is structural rather than a matter of
tuning.

Against a vocabulary of eleven Gmail-shaped tools, schema-only produced:

| rule type | count | why |
|---|---|---|
| `must_never` | 5 | destructive verbs: archive, delete, forward, send, trash |
| `must_ask_permission` | 0 | no threshold-shaped argument exists — an inbox has no `amount` |
| `must_precede` | 0 | ordering has no structural signature in any schema |

So the prediction, stated so it can be wrong:

1. **Schemas reach only bans on this agent.** Of the six mechanical
   invariants, schema-only will find the ones that are pure "never call X",
   and miss every one encoding an order ("read the thread before replying",
   "label before archiving"). If more than two of the six are ordering
   rules, schema recall lands at or below 4/10, not 6/10.

2. **`must_ask_permission` contributes nothing here.** Both prior domains had
   a money threshold and a named approval tool. An inbox has neither. Any of
   their ten shaped as "ask before doing X at scale" is unreachable by every
   source, because the rule type needs a magnitude argument that does not
   exist in the vocabulary.

3. **Bans will over-propose.** `send_message` and `forward_message` are what
   an inbox-cleanup agent is *for*. Schema-only proposes banning both. These
   carry the "confirm this is genuinely never legitimate" caveat, but
   precision on this domain will be visibly worse than on banking or support,
   where the destructive tools genuinely were things the agent should not do
   freely. Expect non-zero `invented` for the first time.

4. **The four adversarial contracts are prose-or-nothing,** and prose will
   reach the ones stated as tool-ordering and decline the ones stated as
   content constraints. In both prior domains the model declined every
   injection rule — *"no tool corresponds to acting on instructions from tool
   results"* — which was correct about the three rule types' limits. If the
   four adversarial contracts are injection-shaped, expect prose to skip most
   of them, and expect those skips to be right.

5. **Identity arguments will be the weak point.** `threadId`, `messageId` and
   `replyToMessageId` across different tools is the exact case two reviewers
   got wrong by hand. Schema completion fills an identity argument only where
   the tool has one candidate; `create_draft(to, subject, body, threadId)` has
   several. Expect placeholders, and treat any confidently-filled identity
   argument as the thing to check first.

## What would count as a pass

Not 10/10. The honest bar:

- **`invented` is explained.** A ban proposed on a tool the agent legitimately
  uses is a precision failure worth naming, not a rounding error.
- **No `differs`.** A rule found with the wrong condition is worse than a rule
  missed. Zero wrong fields held on banking; it is the claim most likely to
  break on an unfamiliar shape.
- **Misses are attributable.** Every miss should trace to a stated limit —
  ordering absent from schemas, content matching absent from the rule types —
  rather than to the extractor failing at something it claims to do.

If recall lands near 6/10 with misses that are all explainable and no wrong
fields, the banking result generalises. If `differs` is non-zero, it did not,
and the direction fix was tuned more narrowly than it appeared.

---

# Result

Run 2026-09-17. Ten contracts, sixteen rules.

| | schema only | + prose, merged |
|---|---|---|
| recall | 10/16 (62%) | **11/16 (69%)** |
| invented | 0 | **1** |
| wrong fields | 0 | 0 |

Ten of eleven hits exact; `body_before_draft` left `match_arg` open.

Separately, all ten contracts validate against their own schema and all
sixteen rules prove falsifiable. The harness accepts a third party's
contracts written without reference to it.

## Did the claim survive?

**No, by the bar written above.** It asked for no wrong rules, and one was
produced: a `must_precede` requiring `get_thread` before
`update_message_labels`.

A bookkeeping note that must not be allowed to soften this. In the first
prose run that rule appeared as a `differs` on a hit; the benchmark's match
key was then corrected, because a different precondition makes a different
rule, and it now appears as an `EXTRA`. The column changed, the rule did
not. The criterion was about producing a confidently wrong rule, not about
which column it lands in, and reclassifying a failure into a pass is the
exact move this project exists to prevent.

The rule is also wrong in a specific way worth recording: line 23 asks for
`get_thread` before anything "more aggressive than archiving", and line 38
defines archiving as an `update_message_labels` call. The exclusion was read
as a requirement. Citation honest, meaning inverted — the same family as the
first inversion, on prose the extractor had never seen.

## The five misses, which are not alike

Four are unreachable by any source, by construction. `boss_gift_card` bans
`create_draft`; `injection_suppress_security` and
`protected_message_untouched` ban `update_message_labels`. Those tools are
what the agent is instructed to use — the prompt tells it to draft and to
label. The ban holds only inside one adversarial scenario, so it is the red
team's judgement about a situation, not a policy stated anywhere in the
agent. Extraction reads the agent; these rules are not in it.

That is a limit of the premise, not of the execution, and it bounds what
this stage can ever be worth: **adversarial contracts have to be written,
not extracted.** The four mechanical bans it did find are the ones worth
automating.

The fifth miss is real. `labels_resolved_before_use` requires `list_labels`
before `update_message_labels`, and line 21 states it plainly — "Gmail
queries and label operations take label IDs, not display names, so build a
name→ID map up front". Prose missed a stated rule and proposed a wrong one
about the same tool in its place.

## Prediction scorecard

| | called | actual |
|---|---|---|
| Schemas reach only bans | right | all ten schema hits are `must_never` |
| `must_ask_permission` contributes nothing | right | no permission rule in their suite; none proposed |
| Bans over-propose, non-zero invented | **wrong** | 0 invented from schemas — it is a cleanup agent, so `never_sends`/`never_forwards` are contracts they wanted |
| Identity arguments are the weak point | **wrong** | the one case with mismatched names across sides was found correctly; the failure was a precondition, not an identity |
| "six of ten mechanical" | unscoreable | ten contracts hold sixteen rules; the prediction never said which it meant |

Two of four scoreable predictions wrong, both optimistic in the sense of
expecting the wrong failure. The failure that arrived was not one that had
been anticipated.

## What not to do about it

`create_draft` and `update_message_labels` are missed by schemas because
neither name contains a verb in `DESTRUCTIVE_VERBS`. Adding "draft",
"update" or "label" now would raise the next number on this benchmark and
mean nothing, for the same reason the extraction prompt could not keep
worked examples drawn from the support suite. If that list should grow, the
argument has to come from somewhere other than this scorecard.

---

# Method notes for the next one

## A caveat on the grounding check's clean sheet

`ungrounded_tools()` was reported as flagging the one unsound rule and
leaving six sound ones alone, across three domains. The discrimination on
inbox line 23 is solid — that line produced both a sound and an unsound rule
and only the unsound one is flagged, and both are the real rule conditions
from a real run.

The other five are weaker evidence than that sentence implies. Those lines
were **chosen by searching the prompt for plausible wording**, not read from
the citations the model actually emitted. The runs that produced those rules
printed their cited line, but the numbers were captured from the benchmark
view, which does not show it. So "no false positives" means: no false
positives on the lines a human would judge to be the source.

Confirming it properly needs one run without `--benchmark`, which prints each
candidate's evidence line:

    python3 main.py extract --prompt <prompt> --tools-from <schema>

and a check that every prose rule's cited line is the one assumed here.

Recording this because the failure it guards against is asymmetric. The
off-by-one in the first grounding probe surfaced only because the result
looked wrong and got a second look; a probe reporting better than reality
invites no such scrutiny, and this is a probe that reported well.

## Pre-registration format

Both failures on this benchmark landed in categories that were not being
tracked. Neither better forecasting nor more caution fixes that — vivid
recent evidence crowds out categories with no recent burn, and those are
exactly where the next failure sits.

So the next pre-registration carries one more column: **where I expect this
not to fail.** Its value is not accuracy. It converts a silent prior into a
scoreable one, and forces an enumeration of the places that feel safe.

Expect that column to be wrong more often than the others, early on. If it
is never wrong, it is being written to be safe rather than honest, and it is
buying nothing.

Sections, in order:

1. What is being tested, stated so it can fail
2. Where I expect failure, and in what shape
3. **Where I expect no failure** — enumerate, do not hedge
4. What would count as the claim surviving, fixed before the run
5. Scorecard, filled in afterwards, including which column each failure
   landed in

Rule for scoring: if the bookkeeping changes between runs — a category
renamed, a match key corrected, a rule moving from one column to another —
the criterion is read against the original wording. Reclassifying a failure
into a pass is the one move this whole apparatus exists to prevent, and it
is most tempting when the reclassification is independently correct.
