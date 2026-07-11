# cost

> **Token-usage & USD cost report for AI agent sessions — pi, Claude Code, Codex, opencode**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Powered by Gaia](https://img.shields.io/badge/powered%20by-Gaia%20skill--tree-6b46c1)](https://gaiaskilltree.com)
[![Prices by LiteLLM](https://img.shields.io/badge/prices%20by-BerriAI%2Flitellm-0ea5e9)](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-3776ab.svg)](https://www.python.org)
[![Zero deps](https://img.shields.io/badge/deps-stdlib%20only-success)](#requirements)
[![Prices auto-refresh](https://github.com/gaia-research/skill-cost/actions/workflows/refresh-prices.yml/badge.svg)](./.github/workflows/refresh-prices.yml)

> **Prices sourced from [BerriAI/litellm](https://github.com/BerriAI/litellm) (MIT).**
> **[Powered by Gaia](https://gaiaskilltree.com) — an open registry of AI agent skills.**

**How much did that agent session actually cost?**

Your harness writes a JSONL session log on every turn. It records token counts. It often does *not* record dollars — pi records `cost: 0`, self-hosted proxies rarely price anything, and the "cost" number many CLIs surface is stale by a version. `cost` reads the raw token counts, prices them against [LiteLLM's canonical catalog](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json), and gives you the real number.

No pip. No npm. No config. **One Python file, one JSON price sheet, prices auto-refresh every 7 days.**

---

## Install

```bash
bash <(curl -sL https://raw.githubusercontent.com/gaia-research/skill-cost/main/install.sh)
```

Auto-detects `~/.pi/agent/skills`, `~/.claude/skills`, `~/.codex/skills`, `.agents/skills`, or `.claude/skills`. Ships a current `prices.json`, then keeps it fresh silently.

### Or grab the script

```bash
curl -sL https://raw.githubusercontent.com/gaia-research/skill-cost/main/cost.py     -o cost.py
curl -sL https://raw.githubusercontent.com/gaia-research/skill-cost/main/prices.json -o prices.json
python3 cost.py
```

---

## Set-and-forget: how updates work

You install once. After that:

| Layer                    | Update mechanism                                                                                                          | Frequency        |
|--------------------------|---------------------------------------------------------------------------------------------------------------------------|------------------|
| **Prices** (`prices.json`) | On invoke: if the local file is >7 days old, `cost.py` silently re-fetches from LiteLLM. Network failure → keep cache.  | Daily (upstream) / weekly (client) |
| **Prices in this repo**  | GitHub Actions cron runs `--refresh-prices` daily and commits any diff. Fresh installs are always current on day one.     | Daily            |
| **Script logic**         | Rarely changes. Re-run the one-line installer to upgrade. No silent self-update (security).                               | On demand        |

No cron on your machine. No background daemon. No `pip install --upgrade`. Set `SKILL_COST_NO_AUTO_REFRESH=1` if you want to freeze the price sheet.

---

## Run

```bash
# [default] the currently active session (newest-mtime JSONL across all harnesses)
python3 cost.py

# Latest session per harness in current cwd
python3 cost.py --latest

# Every session across every harness
python3 cost.py --all --list

# Today only, with per-model breakdown
python3 cost.py --today --by-model

# Since a date, one harness only
python3 cost.py --since 2026-07-01 --harness claude-code

# Filter by working directory or session-id substring
python3 cost.py --cwd "$PWD"
python3 cost.py --session 019f4e66

# Machine-readable output
python3 cost.py --json

# Prices
python3 cost.py --refresh-prices    # force a refresh right now
python3 cost.py --offline           # skip network entirely
```

Environment knobs:

- `SKILL_COST_MAX_AGE_DAYS=N` — auto-refresh threshold (default `7`)
- `SKILL_COST_NO_AUTO_REFRESH=1` — freeze prices; never touch the network

---

## Sample output

```
-- pi  019f4e66-4216-74cc-834b-6633b275a278
   cwd:    C:\Users\me\projects\web
   time:   2026-07-10T23:39:12.278Z  ->  2026-07-10T23:57:10.128Z
   file:   ~/.pi/agent/sessions/--C--Users-me-projects-web--/2026-07-10T23-39-12-278Z_019f4e66-....jsonl
   tokens: in=3,552 out=19,986 cache_r=843,710 cache_w=72,737 total=939,985
   cost:   $1.6104

------------------------------------------------------
1 session(s)   tokens=939,985   cost=$1.6104
```

The number matches whatever your harness *would* have billed against the canonical Anthropic/OpenAI rate card, including cache-read and cache-write pricing.

---

## Supported harnesses

Auto-detected from `$HOME`:

| Harness      | JSONL root                              | Verified                     |
|--------------|-----------------------------------------|------------------------------|
| pi           | `~/.pi/agent/sessions/**/*.jsonl`       | ✓                            |
| Claude Code  | `~/.claude/projects/**/*.jsonl`         | ✓                            |
| OpenAI Codex | `~/.codex/sessions/**/*.jsonl`          | best-effort; PRs welcome     |
| opencode     | `~/.local/share/opencode/**/*.jsonl`    | best-effort; PRs welcome     |

Adding a new harness is a ~15-line `parse_*` function in `cost.py` and a new entry in the `HARNESSES` dict. See the `parse_pi` and `parse_claude_code` implementations for the pattern.

---

## Why prices from LiteLLM

The [`model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) file in the [BerriAI/litellm](https://github.com/BerriAI/litellm) repo (MIT) is the *de facto* community-maintained price catalog for LLM APIs. **2,900+ models across 100+ providers**, updated within days of any provider price change, plain JSON, no auth. `cost` prunes it to just the fields we need and ships the pruned copy (~430 KB) with the skill.

Every refresh writes a `_meta` block into `prices.json` recording the source URL, license, and fetch timestamp — so the provenance always travels with the data. The same info is echoed on the last line of every `cost.py` run and included under `.prices` in `--json` output.

> **If you use this in a paper, dashboard, or blog post, cite both projects:**
> - [BerriAI/litellm](https://github.com/BerriAI/litellm) — the underlying price catalog (MIT)
> - [gaia-research/skill-cost](https://github.com/gaia-research/skill-cost) — this tool (MIT, see [CITATION.cff](./CITATION.cff))

Alternatives considered:
- Scraping `anthropic.com/pricing` weekly — fragile, no OpenAI/others, no cache rates
- Hardcoding a table — the entire reason this repo exists
- Anthropic API `cost` fields — pi + many proxies emit `0`

---

## As an agent skill

Once installed, invoke from any agent conversation:

```
/cost                # current session
/cost --all --list   # every session, one line each
/cost --today        # today's activity
```

The agent reads `SKILL.md`, runs the script, and shows the report.

---

## Requirements

| Requirement    | Notes                                        |
|----------------|----------------------------------------------|
| **Python 3.8+** | stdlib only — no `pip install` steps        |
| Network        | Only for auto-refresh; `--offline` skips it |

---

## How it compares

| Tool | Focus | Setup | Multi-harness | Prices auto-update |
|---|---|---|---|---|
| **`cost`** | Any harness JSONL → USD, one file | `curl \| bash` | pi, Claude Code, Codex, opencode | Yes — LiteLLM catalog, 7-day refresh |
| [ccusage](https://ccusage.com) | Claude Code / Codex / opencode / Amp / Droid / Codebuff dashboards | `npx ccusage` | Yes | Yes |
| Harness built-in cost | Whatever the harness emits | Zero | Single-harness | Whatever the vendor ships |
| Hand-maintained pricing table | Manual | You write it | You maintain it | No — goes stale in weeks |

---

## Contributing

New harness support = a `parse_*` function + `HARNESSES` entry + a note in the compatibility table. PRs welcome.

If a model shows as `unpriced`, run `python3 cost.py --refresh-prices`. If it's still unpriced after that, it's not in LiteLLM's catalog yet — please [open an issue](https://github.com/BerriAI/litellm/issues/new) against LiteLLM. That fixes it for every tool that reads their catalog.

---

## License & attribution

- **skill-cost** — MIT, see [LICENSE](./LICENSE).
- **Price data** — sourced from [BerriAI/litellm](https://github.com/BerriAI/litellm) (MIT). We do not maintain the catalog; we consume and prune it. Every price fix belongs upstream.
- **Powered by [Gaia](https://gaiaskilltree.com)** — an open registry of AI agent skills.

---

<a href="https://gaiaskilltree.com"><img src="./powered-by-gaia.svg" alt="Powered by Gaia" height="28"></a>
