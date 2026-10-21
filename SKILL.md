---
name: cost
description: >-
  Report token usage and USD cost for AI agent sessions by parsing logs from
  pi, Claude Code, OpenAI Codex, opencode, and Hermes Agent. Prices are
  auto-refreshed weekly from the LiteLLM catalog. Use when the user asks
  about cost, spend, token usage, "how much did this session cost", or
  invokes /cost.
version: 1.2.0
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
(required together), `--offline`.

### Role map file

A flat JSON object from a session id or agent id to a role name. Keys that start
with `_` are comments and are ignored. Agent ids may be written bare
(`ace1f9a8b8a8e5f08`), as `agent-<id>`, or as `<id>.jsonl`.

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
  `message.id` + `requestId`. Streaming copies of one call collapse to the largest.
- **Public-rate USD** (`publicRateUsdEquivalent`) is the same tokens priced at the
  catalog rate. It is an equivalent, not a bill. Pricing uses exact catalog keys
  (after the same name normalization as the normal view), with no substring
  fallback. A model with no exact entry is listed in `unknownPrices` with its tokens and
  no USD, and it is never priced with a different model's rate.
- **Actual billed spend** (`actualBilledSpend`) is always `null` with
  `reason: "not measured by this tool"`, unless the caller supplies `--billed-usd` and
  `--billed-source`. Those values are echoed with that provenance and never checked.
- **Subscriptions.** Claude Code Pro/Max usage is not billed per token. Never present
  the USD figure as an extra API charge. The `disclaimer` field and the markdown
  footer say so.
- **Completeness.** A referenced subagent with no transcript, a role-map key that
  matches nothing, an unreadable file, or an unparsable JSON line sets
  `complete: false` and records the reason under `incompleteReasons` and
  `inaccessible[]`. The report is still written, and the exit code stays 0 unless
  `--strict` is set, in which case it is 3. Numbers are never invented to fill a gap.
- **Advisor.** Records can carry `advisorModel`. That field is metadata, not usage.
  If no usage record for it exists, the report warns that advisor tokens are not
  visible in the logs.
- **Provenance.** Each run records `prices.json` sha256, source URL and fetch
  time, the tool version, the generation time, and the log files read (relative
  paths with sha256, size and mtime).

### How a Gaia Steward campaign calls it

After a campaign's sessions finish, run it once for the lead session, with a role
map that names each builder and verifier by agent id:

```bash
python3 cost.py --run-report --json --offline --session <lead-session-id> --role-map <campaign-roles.json>
```

Paste the markdown rendering (`--markdown`) into the campaign issue or PR comment.
Do not hand-write the numbers, and do not add a billed figure unless the invoice was
read. If the report says `complete: false`, state that in the comment and list the
`inaccessible[]` entries. Do not present the totals as complete.

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
