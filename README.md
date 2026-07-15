# cost

> **Token-usage & USD cost report for AI agent sessions — pi, Claude Code, Codex, opencode, Hermes Agent**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Powered by Gaia](https://img.shields.io/badge/powered%20by-Gaia%20skill--tree-6b46c1)](https://gaiaskilltree.com)
[![Prices by LiteLLM](https://img.shields.io/badge/prices%20by-BerriAI%2Flitellm-0ea5e9)](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-3776ab.svg)](https://www.python.org)
[![Zero deps](https://img.shields.io/badge/deps-stdlib%20only-success)](#requirements)
[![Prices auto-refresh](https://github.com/gaia-research/skill-cost/actions/workflows/refresh-prices.yml/badge.svg)](./.github/workflows/refresh-prices.yml)

> **Prices sourced from [BerriAI/litellm](https://github.com/BerriAI/litellm) (MIT).**
> **[Powered by Gaia](https://gaiaskilltree.com) — an open registry of AI agent skills.**

**How much did that agent session actually cost?**

Your harness persists token counts in JSONL logs or a local session database. It often does *not* record dollars — pi records `cost: 0`, self-hosted proxies rarely price anything, and the "cost" number many CLIs surface is stale by a version. `cost` reads the persisted token counts and prices them against [LiteLLM's canonical catalog](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json), producing a reproducible public-rate estimate. Subscription-included or negotiated billing may differ from this estimate.

No pip. No npm. No config. **One Python file, one JSON price sheet, prices auto-refresh every 7 days.**

---

## Why this exists

I kept asking my agent, at the end of long orchestration sessions, "how much did that cost?" It would say things like `~$2` with high confidence. I believed it for a while.

Then I checked the raw token counts. One session it had estimated at $2 was closer to $8 — 4× off. Another one where it reported "under a dollar" had burned through a few million cache-read tokens that its estimate had silently dropped.

There is no single bug. There are several, and they compound:

- **The harness often doesn't know the price.** pi records `cost: 0` because it routes through a proxy that doesn't price. Claude Code's own display uses whatever rate card was baked into the CLI version you installed — which goes stale within weeks of the next Anthropic price change.
- **Agents that estimate on request will hallucinate the rate card.** When you ask a model "what did this session cost?", it does not read `prices.json`. It guesses, from training-cutoff-era numbers, using whichever of `input`/`output`/`cache_read`/`cache_write` it happens to remember. Cache-read alone was consistently forgotten in my sessions and it is often the largest bucket.
- **Long sessions compact.** After a compaction, an agent looking at only the tail of the conversation genuinely does not see the pre-compaction turns. It reports the cost of the last chunk and calls it "the session."

So the number you see mid-conversation is decoration. The real bill comes later, from the vendor, and by then the session is over.

`cost` reads the harness's persisted usage totals, including cache-read and cache-write tokens across compactions, and prices them against LiteLLM's current rate card. The result is a reproducible public API-equivalent estimate; actual spend can differ for subscriptions, credits, free tiers, custom contracts, and local models.

---

## Install

```bash
bash <(curl -sL https://raw.githubusercontent.com/gaia-research/skill-cost/main/install.sh)
```

Auto-detects `~/.pi/agent/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.hermes/skills`, `.agents/skills`, or `.claude/skills`. Ships a current `prices.json`, then keeps it fresh silently.

### Or grab the script

```bash
curl -sL https://raw.githubusercontent.com/gaia-research/skill-cost/main/cost.py     -o cost.py
curl -sL https://raw.githubusercontent.com/gaia-research/skill-cost/main/prices.json -o prices.json
python3 cost.py
```

---

## Cheat sheet

As a slash-command inside an agent conversation (once installed as a skill):

| Command | What it shows |
|---|---|
| `/cost` | The newest logical session across every harness. Default view. |
| `/cost --latest` | The latest session per harness in the current `cwd`. Useful when several harnesses have logged from the same project. |
| `/cost --today` | Everything that touched the wire today (UTC). |
| `/cost --since 2026-07-01` | Everything since a date. Use `YYYY-MM-DD` or a full ISO timestamp. |
| `/cost --since 2026-07-01 --by-model` | Same, with a per-model breakdown so you can see whether Opus or Sonnet is doing the damage. |
| `/cost --cwd $PWD` | Only sessions whose recorded `cwd` matches this directory. "What has this project cost me?" |
| `/cost --harness pi` | Restrict to one harness. Also: `claude-code`, `codex`, `opencode`, `hermes`. |
| `/cost --session 019f4e66` | Substring match on session id. Useful when a coworker sends you a session id. |
| `/cost --all --list` | Every session on the machine, one line each. Sorted by recency. |
| `/cost --all --list --today` | Today, one line each. Fits on a terminal. |
| `/cost --json` | Machine-readable. Pipe to `jq` for scripting or dashboards. |
| `/cost --refresh-prices` | Force-fetch the latest rate card from LiteLLM right now. Bypasses the 7-day cache. |
| `/cost --offline` | Skip network entirely. Use the bundled `prices.json` as-is. |

At the shell it is the same commands with `python3 cost.py` in place of `/cost`.

Environment knobs:

- `SKILL_COST_MAX_AGE_DAYS=N` — how stale the local `prices.json` may get before auto-refresh (default 7)
- `SKILL_COST_NO_AUTO_REFRESH=1` — disable auto-refresh entirely; freeze the rate card

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

Every flag from the cheat sheet above works at the shell too — substitute `python3 cost.py` for `/cost`. `python3 cost.py --help` for the full argument list.

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

The number estimates what the same tokens would cost against the canonical public Anthropic/OpenAI rate card, including cache-read and cache-write pricing. It is not a claim about subscription-included or contract billing.

---

## Supported harnesses

Auto-detected from `$HOME`:

| Harness      | Session storage                         | Status        |
|--------------|-----------------------------------------|---------------|
| pi           | `~/.pi/agent/sessions/**/*.jsonl`       | **Verified in production** — primary development target; compaction detection tested against live sessions |
| Claude Code  | `~/.claude/projects/**/*.jsonl`         | **Experimental** — parser runs cleanly against 1,000+ real sessions on the maintainer's machine, but per-session $ figures have not yet been reconciled against Anthropic invoice line items |
| OpenAI Codex | `~/.codex/sessions/**/*.jsonl`          | **Schema-only** — parser written from public Codex JSONL docs; no live-session validation yet. PRs welcome |
| opencode     | `~/.local/share/opencode/**/*.jsonl`    | **Schema-only** — parser written from opencode's assistant-message shape; no live-session validation yet. PRs welcome |
| Hermes Agent | `$HERMES_HOME/state.db`                 | **Schema-verified (v20)** — reads aggregate and per-model usage in SQLite read-only mode; never reads message content |

### What "verified" actually means here

- **Verified in production** — the maintainer runs `/cost` against this harness daily. Compaction events are detected. Token totals match the harness's own reported context-window usage where it exposes one. Model-name normalization is exercised across the current Claude 4.x family.
- **Experimental** — the JSONL schema is stable and parsing is non-crashing across a large corpus of real logs. Numbers are believed correct (they use the same LiteLLM per-token rates and the same cache-read / cache-creation fields Anthropic bills against), but no one has yet cross-checked them against an actual paid invoice for that harness. Treat the number as directionally correct, not audit-grade.
- **Schema-only** — the parser is a best-effort implementation from public docs / source. It probably works. It has not been run against a real session on the maintainer's machine. Please open an issue with an example JSONL line if the numbers look off.

Adding a new line-oriented harness is a small `parse_*` function in `cost.py` and a new entry in the `HARNESSES` dict. SQLite-backed harnesses can provide a loader like Hermes. See `parse_pi`, `parse_claude_code`, and `load_hermes_db` for the patterns.

### Hermes `/usage` vs `/cost`

Hermes already provides `/usage` for the active session and `hermes insights`
for native historical analytics. Keep using those for live Hermes telemetry.
`/cost` is complementary: it reads the same persisted token metadata in
SQLite read-only mode and reprices it with the same LiteLLM catalog used for
pi, Claude Code, Codex, and opencode. That makes cross-harness and
project-filtered comparisons reproducible without reading transcripts. Only
the active `$HERMES_HOME` is inspected; named profiles remain isolated unless
the caller explicitly selects one.

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
| **`cost`** | Harness logs → USD, one file | `curl \| bash` | pi, Claude Code, Codex, opencode, Hermes | Yes — LiteLLM catalog, 7-day refresh |
| [ccusage](https://ccusage.com) | Claude Code / Codex / opencode / Amp / Droid / Codebuff dashboards | `npx ccusage` | Yes | Yes |
| Harness built-in cost | Whatever the harness emits | Zero | Single-harness | Whatever the vendor ships |
| Hand-maintained pricing table | Manual | You write it | You maintain it | No — goes stale in weeks |

---

## Behaviors worth knowing

### Compaction is detected but not double-billed

When your agent compacts context (pi writes `type: "compaction"`, Claude Code sets `isCompactSummary: true`), `cost` records the event but does not add anything to your bill for the checkpoint itself. Every pre- and post-compaction turn is already billed independently from its own `usage` block. The `compact:` line in the report just tells you *how many* times it happened and, for pi, *how deep* the context was at each checkpoint (`tokens_before`) so you can spot a session that's thrashing the window.

### Cache pricing follows the vendor

When the LiteLLM catalog carries `cache_read_input_token_cost` and `cache_creation_input_token_cost` for a model, `cost` uses those numbers verbatim. If a model has no cache-rate entry (rare, mostly non-Anthropic), `cost` falls back to Anthropic's public ratios (`0.10× input` for reads, `1.25× input` for ephemeral-5m writes). Any actual figure you see is either the vendor's published rate or clearly flagged as unpriced.

### The reported number is what the vendor *would* have billed

Harnesses like pi that route through a proxy typically emit `cost: 0` in their JSONL because the proxy doesn't price. `cost` deliberately ignores that field and recomputes from token counts against LiteLLM rates — so the number matches what the underlying vendor would have charged for the same call, regardless of whether *you* paid it, your employer's SAP tenancy paid it, or the request was on a free tier.

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
