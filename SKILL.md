---
name: cost
description: >-
  Report token usage and USD cost for AI agent sessions by parsing logs from
  pi, Claude Code, OpenAI Codex, opencode, and Hermes Agent. Prices are
  auto-refreshed weekly from the LiteLLM catalog. Use when the user asks
  about cost, spend, token usage, "how much did this session cost", or
  invokes /cost.
version: 1.3.0
---

# cost — Multi-Harness Token Cost Report

Reads session usage records written by AI agent harnesses on this machine
and reports persisted token usage plus a **public-rate USD estimate** per session. Prices come
from [BerriAI/litellm](https://github.com/BerriAI/litellm)'s canonical
`model_prices_and_context_window.json` catalog and auto-refresh weekly.

## Supported harnesses

Auto-detected from `~`:

| Harness      | Session storage                         |
|--------------|-----------------------------------------|
| pi           | `~/.pi/agent/sessions/**/*.jsonl`       |
| Claude Code  | `~/.claude/projects/**/*.jsonl`         |
| OpenAI Codex | `~/.codex/sessions/**/*.jsonl`          |
| opencode     | `~/.local/share/opencode/**/*.jsonl`    |
| Hermes Agent | `$HERMES_HOME/state.db`                 |

Hermes is read through SQLite in `mode=ro`. The parser queries only `sessions`
and `session_model_usage`; it never reads transcript rows from `messages`.

Adding a new line-oriented harness = one small `parse_*` function in `cost.py`.

## Run

```bash
# [default] the newest logical session across all harnesses
python3 cost.py

# Latest session per harness in current cwd
python3 cost.py --latest

# Every session across every harness
python3 cost.py --all --list

# Today only, per-model breakdown
python3 cost.py --today --by-model

# Since a date, one harness only
python3 cost.py --since 2026-07-01 --harness claude-code
python3 cost.py --harness hermes

# Filter by working directory or session id substring
python3 cost.py --cwd "$PWD"
python3 cost.py --session 019f4e66

# Machine-readable
python3 cost.py --json

# Prices
python3 cost.py --refresh-prices    # force a refresh now
python3 cost.py --offline           # skip any network access
```

Environment:

- `SKILL_COST_MAX_AGE_DAYS=N` — refresh threshold (default 7)
- `SKILL_COST_NO_AUTO_REFRESH=1` — disable auto-refresh entirely

## Pricing model

For each `(model, session)` bucket, cost = sum of

```
input_tokens       * input_price
+ output_tokens      * output_price
+ cache_read_tokens  * cache_read_price       (fallback: 0.10 * input_price)
+ cache_write_tokens * cache_creation_price   (fallback: 1.25 * input_price)
```

Rates come from LiteLLM's catalog per token; fallbacks match Anthropic's
public rate card for ephemeral-5m cache.

### Cache hits

A long agent session is overwhelmingly cache reads, and a cache read is billed at a
fraction of the input rate (a tenth, on Anthropic's card). A six-figure token count next
to a three-figure total therefore looks wrong until you can see that split, so every
session prints it:

```
   cache:  98.8% of 46,703,358 prompt tokens served from cache · saved $207.74 vs the same tokens as fresh input
   spend:  in $0.00 · out $1.91 · cache read $23.08 · cache write $3.36
```

- **hit rate** — `cache_read / (input + cache_read + cache_write)`. Every prompt token is
  billed as exactly one of those three, so they are the honest denominator. Output tokens
  are not prompt tokens and are excluded.
- **saved** — the counterfactual: what those cached tokens *would* have cost as ordinary
  input, minus what they did cost. It is a saving against not caching, not a discount off
  the invoice.
- **spend** — the same total, split by billing component, so a surprising number can be
  attributed to a component rather than guessed at.

The run-level footer carries the same two figures across every session shown. Both are
in `--json` as `totals.cache_hit_rate`, `spend_usd`, and `cache_savings_usd`.

Sessions mixing several models sum each component at that model's own rate. A model the
catalog cannot price contributes nothing rather than being guessed at, and is still named
in the `unpriced` note.

## Run report (`--run-report`): one session, attributed by role

`--run-report` reports one Claude Code main session together with every subagent
transcript that belongs to it, grouped by role. It is the report to paste into a
GitHub issue or PR comment. Only Claude Code is supported.

```bash
# Newest main session (subagents are never picked as the main session)
python3 cost.py --run-report --json --offline

# A specific session, as a markdown table
python3 cost.py --run-report --markdown --session 3f42d4e9 --offline

# Attribute builders and verifiers explicitly, fail (exit 3) if anything is missing
python3 cost.py --run-report --json --role-map roles.json --strict

# Echo an actual invoice figure with its provenance
python3 cost.py --run-report --json --billed-usd 12.50 --billed-source "Anthropic invoice 2026-10"
```

Flags: `--session` (substring on the session id), `--cwd` (recorded cwd), `--json`,
`--markdown`, `--role-map FILE`, `--strict`, `--billed-usd N` with `--billed-source TEXT`
(required together), `--offline`. `--markdown`, `--role-map`, `--strict`, `--billed-usd`
and `--billed-source` are valid only with `--run-report`; without it they exit 2 with a
usage message.

Exit codes: `0` report written (complete or not, unless `--strict`); `1` no matching
session, or an unexpected failure; `2` usage error (bad flag combination, unreadable or
invalid role map, a harness other than Claude Code); `3` incomplete with `--strict`.

Run-report stderr is safe to paste with the stdout. It never contains an absolute path,
and it never contains a traceback. An unexpected failure prints one line,
`error: run-report failed (<ExceptionClassName>)`, and exits 1. Warnings about
`prices.json` name the error class or errno only.

### Role map file

A flat JSON object from a session id or agent id to a role name. Keys that start
with `_` are comments and are ignored. Agent ids may be written bare
(`ace1f9a8b8a8e5f08`), as `agent-<id>`, or as `<id>.jsonl`.

A key that names no transcript is listed in `inaccessible[]` with
`kind: "role-map-reference"`, and the report is incomplete. A key that names a transcript
whose usage records contribute no row (for example, every record was a duplicate of a call
already counted from another transcript) does not change the totals, but it adds the
warning `role-map-key-has-no-effect: <key>`.

```json
{
  "_comment": "Haiku F run, 2026-10-21",
  "3f42d4e9-fe78-5e85-9315-ce7984b0b8c9": "lead",
  "ace1f9a8b8a8e5f08": "builder",
  "a2a21c231977f5fa7": "verifier"
}
```

### How roles are assigned

Rules run in this order. The first match wins.

1. **Explicit map.** A session or agent id listed in `--role-map` gets that role.
2. **Lead.** Main-session usage whose model is the main session's dominant model (most
   output tokens) is role `lead`.
3. **Subagent family.** Any other subagent usage is role `subagent:<model>`, for
   example `subagent:claude-haiku-5-5`. Builder and verifier are never guessed from
   the model name, because two Haiku subagents look the same in the logs.
4. **Unattributed.** Other main-session models and usage with no model name go to
   `unattributed`.

### Reading the report

- **Measured tokens** (`measuredTokens`) are read from the logs. Each API call is
  counted once across the main and subagent transcripts. Records are keyed by
  `message.id` + `requestId`, and streaming copies of one call collapse to the largest.
  If any copy of a `message.id` has no `requestId`, every copy with that id is one call.
  When those copies carry two or more distinct `requestId`s, that merge may hide real calls.
  The report then adds a warning `ambiguous-id-only-copy` and an `incompleteReasons` entry
  that says "possible undercount", and sets `complete: false`. Both name the message id only
  by the first 8 hex characters of its sha256, never the id itself.
- **Malformed usage** (`malformedUsage`, and `malformedUsageRecords` per file). A token
  count is valid only as a JSON integer from 0 to 10^15 per field and per record. A usage
  record with any other count (a float, even `2.0`; a boolean; a string; a negative
  number; a larger integer), or with a cache-write split that cannot be placed, is
  excluded from every total. It counts here and sets `complete: false`. Counts are never
  zero-filled or coerced. A cache-write total with a missing 5-minute or 1-hour part is
  split from `cache_creation_input_tokens`. A part with no total to split from is malformed. A
  total with no breakdown at all (an older record that has no `cache_creation` object)
  counts as 5-minute writes, and that split is recorded as an assumption.
- **Public-rate USD** (`publicRateUsdEquivalent`) is the same tokens priced at the
  catalog rate. It is an equivalent, not a bill. Pricing uses exact catalog keys (after
  the same name normalization as the normal view), with no substring fallback. Three
  cases are never filled in silently:
  - A model with no exact entry is listed in `unknownPrices` with
    `reason: "no-catalog-entry"`, and it is never priced with another model's rate.
  - A model whose entry has no output price is listed with
    `reason: "incomplete-price-entry"`. It is not priced as if output were free.
  - A row whose price arithmetic overflows a float is listed with `reason: "overflow"`.
    It is never reported as a finite number.
  - A missing cache price falls back to 10% (cache read), 125% (5-minute cache write)
    or 200% (1-hour cache write) of the input rate. Each fallback, with the tokens it
    covers, is listed in `priceAssumptions[]`. So is a cache-write split that had to be
    inferred, including every total-only record (`component: "cache_write_split"`). A
    catalog value of `0` is kept as `0`.
  `publicRateUsdEquivalent.complete` is `false` whenever `unknownPrices` is non-empty.
  The markdown status then says `USD partial: N unpriced model(s)`, and the total row
  reads `≥ $X (partial)`, meaning a lower bound.
- **Output undercount** (`outputLikelyUndercounted`, `outputUndercount`, and
  `byRole[<role>].outputUndercount`). Claude Code logs can hold early streaming snapshots of
  one API call, and those carry `output_tokens` of 10 or fewer, so measured output is then
  short. The check runs on the deduplicated records (after the `message.id` + `requestId`
  merge above). A record is a **suspect snapshot** when its `output_tokens` is at most 10
  and its prompt side (`input_tokens` + cache read + cache write) is at least 100 tokens.
  The 100-token floor keeps out records with almost no prompt, which say nothing about
  their output. A scope, the whole run or one role, is **flagged** when more than half of
  its records are suspect (`fraction` strictly above 0.5). `suspectRecords`, `totalRecords`
  and `fraction` are reported for the run and for each role. `fraction` is `null` for a
  scope with no records, and such a scope is never flagged. The thresholds are in
  `outputUndercount.rule`. When the run or any role is flagged:
  - `outputLikelyUndercounted` is `true`, and both `complete` and `usdComplete` are `false`.
    An undercount leaves the measured output short, so it clears both.
  - `incompleteReasons` gains `output-likely-undercounted: overall <S> of <T> usage records
    (<P>%) ...`. If only roles are flagged, it gains one `output-likely-undercounted: role
    <role> ...` entry per flagged role. Those entries start with the stable prefix
    `output-likely-undercounted:`, and their counts are also in the JSON blocks above.
  - `warnings`, the markdown, and stderr say, in plain language, that measured output and
    the public-rate USD are a **LOWER BOUND**, and that the provider or account billing
    dashboard is authoritative for billed spend.

  The flag is a signal, not a correction. The report does not estimate the missing output.
  The rule cannot tell an early snapshot from a short final reply, so a run of one or two
  calls can be flagged by a single short reply. Read the counts before taking the flag as
  proof of undercounting.
- **User-supplied billed figure** (`actualBilledSpend`) is `null` with
  `reason: "not measured by this tool"`, unless the caller supplies `--billed-usd` and
  `--billed-source`. The markdown labels it `User-supplied billed figure (unverified)`.
  The value is echoed with that provenance. It is never checked against an invoice or
  against the logs, and the disclaimer says so.
- **Subscriptions.** Claude Code Pro/Max usage is not billed per token. Never present
  the USD figure as an extra API charge. The `disclaimer` field and the markdown
  footer say so.
- **Completeness.** `complete` is `false` (exit 0, or exit 3 with `--strict`) when any
  of these holds. Each is recorded in `incompleteReasons`, and each subagent case also
  in `inaccessible[]`:
  - a subagent is named by a `toolUseResult.agentId` in a transcript (the main session or
    a subagent) and no transcript exists for it;
  - a subagent has a metadata sidecar but no transcript, or a `--role-map` key matches
    no session or subagent;
  - a transcript cannot be read, or a JSON line cannot be parsed;
  - a usage record is malformed (see above);
  - the output undercount flag is set (see above).
  The report is still written. Numbers are never invented to fill a gap.
- **Two completeness flags.** The top-level `complete` means the *measured tokens* are
  complete. It ignores USD. `usdComplete` is the same value as
  `publicRateUsdEquivalent.complete`, copied to the top level for consumers that read only
  that. It is `false` when any model is unpriced, any row overflows, or the output
  undercount flag is set. So a report can read `complete: true` with `usdComplete: false`:
  every token was read, but some USD is a lower bound. The output undercount is the one case
  where `complete` is also `false`, because the measured output is short. The markdown shows
  the USD state on its own status line and does not print `usdComplete`.
- **Advisor.** Records can carry `advisorModel`. That field is metadata, not usage.
  If no usage record for it exists, the report warns that advisor tokens are not
  visible in the logs.
- **Provenance and privacy.** The report contains no absolute paths, so it is safe to
  paste. `session.cwd` is the directory's basename. `session.cwdSha256` is the first 8
  hex characters of the sha256 of the full cwd, so two reports from one directory can
  be correlated. `logPath` and `files[].path` are `<session>.jsonl` or
  `<session>/subagents/<file>`, relative to the projects root and without the encoded
  project directory (that directory name spells out the cwd). Each file entry has its
  sha256, size, mtime, usage-record count and malformed count. The run also records
  the `prices.json` sha256, source URL and fetch time, the tool version, the generation
  time, and the role-map sha256 when one is given.

### Limitations

- `--run-report` reads Claude Code only: the main session and its subagent transcripts.
  Anything outside those files is not measured. Advisor tokens appear only if their
  usage records are in these logs.
- Missing, unreadable or malformed data makes the report `complete: false`. It does not
  estimate. A complete report is a checked total for the logs it read, not a bill.
- An output undercount is detected, not repaired. The report never adds the output that
  the logs do not hold. A flagged run stays `complete: false` and `usdComplete: false` until
  the logs hold the final counts for those calls. This report never reads the provider's
  billing, so the billing dashboard, not this report, settles the billed amount.
- `--role-map` is the only way to tell two same-model subagents apart. Without it they
  share a `subagent:<model>` role.
- **The default `/cost` view is not covered by these rules, and it is unchanged.** It
  shares the Claude Code parser, which keeps its earlier behavior so that default output
  stays byte-identical. In that view, a negative or boolean token count is added as
  is (`true` counts as 1), and a float count is truncated to an integer. A cache-write
  total is dropped when `cache_creation` has no `ephemeral_5m_input_tokens`. One message id with and without a `requestId` is counted
  twice. A usage value that is not an object, or a non-object JSON line, stops the view
  with an `AttributeError`. Use `--run-report` for a checked total of one session.
- The price catalog is LiteLLM's. A model it does not list is reported under
  `unknownPrices`, never guessed. Run `python3 cost.py --refresh-prices` if a new model
  shows up there.

### How a Gaia Steward campaign calls it

After a campaign's sessions finish, run it once for the lead session, with a role
map that names each builder and verifier by agent id:

```bash
python3 cost.py --run-report --json --offline --session <lead-session-id> --role-map <campaign-roles.json>
```

Paste the markdown rendering (`--markdown`) into the campaign issue or PR comment.
Do not hand-write the numbers, and do not add a billed figure unless the invoice was
read. If the report says `complete: false`, state that in the comment and list the
`inaccessible[]` entries and `incompleteReasons`. Do not present the totals as complete.
If `usdComplete` (the same as `publicRateUsdEquivalent.complete`) is `false`, say that the
USD total is a lower bound and name the `unknownPrices` models. If
`outputLikelyUndercounted` is `true`, say that measured output and the USD are lower bounds,
give the `outputUndercount` counts for the run and any flagged role, and say that the
provider or account billing dashboard is authoritative for billed spend.

## Presenting results to the user

When invoked via `/cost`:

1. Run the script with the arguments the user supplied (default to none).
2. Show the script's stdout verbatim in a fenced block.
3. Summarize the grand-total cost and which harness/session it covers.
4. If the output lists any `unpriced` models, suggest
   `python3 cost.py --refresh-prices`.
5. Do not fabricate numbers — always rely on the script's output.

Hermes' built-in `/usage` and `hermes insights` remain the best live telemetry
for Hermes itself. This skill complements them by applying one LiteLLM price
catalog consistently across the active Hermes profile and other harnesses,
historical sessions, project filters, and combined reports. To inspect a named
profile, invoke the script with that profile's `HERMES_HOME`; profiles are never
scanned implicitly.

## Files

- `cost.py` — parser + pricer, stdlib only
- `prices.json` — pruned LiteLLM catalog (auto-refreshed weekly by CI and
  on-demand at runtime). Includes a `_meta` entry with source/license/fetch-time.

## Attribution

Prices are sourced from
[**BerriAI/litellm**](https://github.com/BerriAI/litellm)'s
[`model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
catalog (MIT). We prune it to the fields we need and ship the pruned copy
as `prices.json`. Every catalog refresh writes a `_meta` block with the
source URL, license, and fetch timestamp so the provenance always travels
with the data. If a model is missing or mispriced, the fix belongs
upstream in LiteLLM — that corrects every tool that reads the catalog.

---

Part of the [Gaia Skill Tree](https://gaiaskilltree.com) — an open registry
of AI agent skills. · **Powered by Gaia.**
