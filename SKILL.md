---
name: cost
description: >-
  Report token usage and USD cost for AI agent sessions by parsing the JSONL
  logs written by pi, Claude Code, OpenAI Codex, and opencode. Prices are
  auto-refreshed weekly from the LiteLLM catalog. Use when the user asks
  about cost, spend, token usage, "how much did this session cost", or
  invokes /cost.
version: 1.0.0
---

# cost — Multi-Harness Token Cost Report

Reads the JSONL session logs written by AI agent harnesses on this machine
and reports **accurate token usage and USD cost** per session. Prices come
from [BerriAI/litellm](https://github.com/BerriAI/litellm)'s canonical
`model_prices_and_context_window.json` catalog and auto-refresh weekly.

## Supported harnesses

Auto-detected from `~`:

| Harness      | JSONL root                              |
|--------------|-----------------------------------------|
| pi           | `~/.pi/agent/sessions/**/*.jsonl`       |
| Claude Code  | `~/.claude/projects/**/*.jsonl`         |
| OpenAI Codex | `~/.codex/sessions/**/*.jsonl`          |
| opencode     | `~/.local/share/opencode/**/*.jsonl`    |

Adding a new harness = one small `parse_*` function in `cost.py`.

## Run

```bash
# [default] the currently active session (newest-mtime JSONL)
python3 cost.py

# Latest session per harness in current cwd
python3 cost.py --latest

# Every session across every harness
python3 cost.py --all --list

# Today only, per-model breakdown
python3 cost.py --today --by-model

# Since a date, one harness only
python3 cost.py --since 2026-07-01 --harness claude-code

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

## Presenting results to the user

When invoked via `/cost`:

1. Run the script with the arguments the user supplied (default to none).
2. Show the script's stdout verbatim in a fenced block.
3. Summarize the grand-total cost and which harness/session it covers.
4. If the output lists any `unpriced` models, suggest
   `python3 cost.py --refresh-prices`.
5. Do not fabricate numbers — always rely on the script's output.

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
