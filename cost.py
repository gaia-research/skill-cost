#!/usr/bin/env python3
"""
cost.py — Compute token-usage cost from agent harness session logs.

Supported harnesses (auto-detected):
  - pi           (~/.pi/agent/sessions/**/*.jsonl)
  - claude-code  (~/.claude/projects/**/*.jsonl)
  - codex        (~/.codex/sessions/**/*.jsonl)
  - opencode     (~/.local/share/opencode/**/*.jsonl)
  - hermes       ($HERMES_HOME/state.db; defaults to ~/.hermes/state.db)

Prices are read from `prices.json` shipped next to this script and refreshed
weekly from BerriAI/litellm's canonical model_prices_and_context_window.json
via GitHub Actions (see .github/workflows/update-prices.yml).

Zero dependencies. Python 3.8+, stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

# Force UTF-8 stdout so non-ASCII output works on Windows consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


# =============================================================================
# Pricing
# =============================================================================

PRICES_JSON = Path(__file__).with_name("prices.json")
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
DEFAULT_MAX_AGE_DAYS = 7


def _max_age_days() -> int:
    try:
        return max(0, int(os.environ.get("SKILL_COST_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)))
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def _prices_age_days() -> float:
    if not PRICES_JSON.exists():
        return float("inf")
    import time
    return (time.time() - PRICES_JSON.stat().st_mtime) / 86400.0


def load_prices(offline: bool = False, redact: bool = False) -> dict:
    """Load bundled prices.json. Silently refresh from LiteLLM if stale.

    Refresh is skipped when:
      - offline=True (--offline flag)
      - env SKILL_COST_NO_AUTO_REFRESH=1
      - the on-disk file is newer than SKILL_COST_MAX_AGE_DAYS (default 7)
    A network failure during auto-refresh is non-fatal: we keep using the
    cached file.

    redact=True is the run-report path: warnings name the error class or errno,
    never the absolute path of prices.json, because a run report's stderr is pasted.
    """
    stale = _prices_age_days() > _max_age_days()
    if (stale
        and not offline
        and os.environ.get("SKILL_COST_NO_AUTO_REFRESH") not in ("1", "true", "yes")):
        try:
            return refresh_prices(write=True, quiet=True)
        except Exception as e:
            detail = _error_text(e) if redact else e
            print(f"warning: auto-refresh of prices.json failed ({detail}); "
                  f"using cached copy.", file=sys.stderr)

    if PRICES_JSON.exists():
        try:
            return json.loads(PRICES_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            if redact:
                print(f"warning: could not parse the bundled prices.json "
                      f"({type(e).__name__})", file=sys.stderr)
            else:
                print(f"warning: could not parse {PRICES_JSON}: {e}", file=sys.stderr)
    # No cache, no network -> last-resort fetch (will raise on failure)
    return refresh_prices(write=False)


def refresh_prices(write: bool = True, quiet: bool = False) -> dict:
    """Fetch the latest LiteLLM catalog and (optionally) write prices.json."""
    if not quiet:
        print(f"fetching prices from {LITELLM_URL} ...", file=sys.stderr)
    req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "skill-cost"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        raw = r.read().decode("utf-8")
    data = json.loads(raw)
    # Prune to fields we actually need -- keeps the shipped file tiny.
    pruned = {}
    for name, m in data.items():
        if not isinstance(m, dict):
            continue
        inp = m.get("input_cost_per_token")
        out = m.get("output_cost_per_token")
        if inp is None and out is None:
            continue
        pruned[name] = {
            "input":      inp,
            "output":     out,
            "cache_read": m.get("cache_read_input_token_cost"),
            "cache_write": m.get("cache_creation_input_token_cost"),
            # Anthropic bills a 1-hour cache write at 2x input, not the 1.25x
            # that a 5-minute write costs. Keep the two rates apart.
            "cache_write_1h": m.get("cache_creation_input_token_cost_above_1hr"),
            "provider":   m.get("litellm_provider"),
        }
    # Embed source attribution so the data always travels with its provenance.
    pruned["_meta"] = {
        "source": "BerriAI/litellm model_prices_and_context_window.json",
        "source_url": LITELLM_URL,
        "source_license": "MIT",
        "source_repo": "https://github.com/BerriAI/litellm",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_count": len(pruned),
        "note": "Pruned to input/output/cache_read/cache_write/cache_write_1h/provider. "
                "Consumed by skill-cost (https://github.com/gaia-research/skill-cost).",
    }
    if write:
        PRICES_JSON.write_text(
            json.dumps(pruned, indent=2, sort_keys=True), encoding="utf-8"
        )
        if not quiet:
            print(f"wrote {len(pruned) - 1} models to {PRICES_JSON}", file=sys.stderr)
    return pruned


# Model-name normalizers: transform harness-specific names into the shape
# LiteLLM uses as its catalog key. Applied in order; first hit wins.
NORMALIZERS: list[Callable[[str], str]] = [
    # pi's `anthropic--claude-4.7-opus` -> `claude-opus-4-7`
    lambda s: re.sub(
        r"^anthropic--claude-(\d+)\.(\d+)-(opus|sonnet|haiku)$",
        lambda m: f"claude-{m.group(3)}-{m.group(1)}-{m.group(2)}",
        s,
    ),
    # Hermes/direct providers: `anthropic/claude-opus-4.8` -> `claude-opus-4-8`
    lambda s: re.sub(
        r"^(?:[a-z_0-9-]+/)*(claude-(?:opus|sonnet|haiku)-\d+)\.(\d+)(.*)$",
        lambda m: f"{m.group(1)}-{m.group(2)}{m.group(3)}",
        s,
    ),
    # Strip provider prefixes like `openrouter/anthropic/…`, `azure_ai/…`
    lambda s: re.sub(r"^[a-z_0-9]+/(anthropic/|openai/)?", "", s),
    # Drop trailing @date or -vN:0 suffixes
    lambda s: re.sub(r"[@-]?v?\d+:\d+$", "", s),
]


def price_for(model: str, prices: dict) -> dict | None:
    """Look up a model in the pricing catalog with progressive fallback."""
    if not model or model == "_meta":
        return None
    candidates = [model, model.lower()]
    for fn in NORMALIZERS:
        try:
            candidates.append(fn(model.lower()))
        except Exception:
            pass
    # 1. Exact match on any candidate
    for c in candidates:
        if c in prices and c != "_meta":
            return prices[c]
    # 2. Longest-substring fallback across the catalog
    best = None
    ml = model.lower()
    for key, p in prices.items():
        if key == "_meta":
            continue
        kl = key.lower()
        if kl in ml or ml in kl:
            score = min(len(kl), len(ml))
            if best is None or score > best[0]:
                best = (score, p)
    return best[1] if best else None


def exact_price_for(model: str, prices: dict) -> dict | None:
    """Exact-key lookup only (after name normalization), with no substring fallback.
    Run reports use this so a model is never priced with a different model's rate."""
    if not model or model == "_meta":
        return None
    candidates = [model, model.lower()]
    for fn in NORMALIZERS:
        try:
            candidates.append(fn(model.lower()))
        except Exception:
            pass
    for c in candidates:
        if c != "_meta" and isinstance(prices.get(c), dict):
            return prices[c]
    return None


# =============================================================================
# Aggregation
# =============================================================================

@dataclass
class Bucket:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # Anthropic's 1-hour prompt-cache writes are billed at 2x input, twice the
    # 1.25x that a 5-minute write costs, so they are counted apart.
    cache_write_1h: int = 0

    def add(self, o: "Bucket") -> None:
        self.input += o.input
        self.output += o.output
        self.cache_read += o.cache_read
        self.cache_write += o.cache_write
        self.cache_write_1h += o.cache_write_1h

    def total_tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write + self.cache_write_1h

    def cost(self, model: str, prices: dict, lookup: Callable | None = None) -> float | None:
        parts = self.cost_parts(model, prices, lookup)
        if parts is None:
            return None
        return (parts["input"] + parts["output"] + parts["cache_read"]
                + parts["cache_write"] + parts["cache_write_1h"])

    def cost_parts(self, model: str, prices: dict, lookup: Callable | None = None) -> dict | None:
        """Cost split by billing component, plus what the cache reads would have cost
        at full input price. A total that looks surprising is nearly always volume
        rather than rate, and the only way to see that is to break it apart.
        `lookup` defaults to price_for; run reports pass exact_price_for so that a
        model is never priced with another model's rate."""
        p = (lookup or price_for)(model, prices)
        if not p or p.get("input") is None:
            return None
        inp = float(p["input"])
        out = float(p.get("output") or 0)
        cr  = float(p.get("cache_read")  or inp * 0.10)
        cw  = float(p.get("cache_write") or inp * 1.25)
        cw1h = float(p.get("cache_write_1h") or inp * 2.0)
        return {
            "input":       self.input       * inp,
            "output":      self.output      * out,
            "cache_read":  self.cache_read  * cr,
            "cache_write": self.cache_write * cw,
            "cache_write_1h": self.cache_write_1h * cw1h,
            # Counterfactual: the same cached prompt tokens billed as ordinary input.
            "uncached_equivalent": self.cache_read * inp,
        }

    def prompt_tokens(self) -> int:
        """Every prompt token is billed as exactly one of uncached input, a cache read,
        or a cache write -- so these three are the denominator for a hit rate."""
        return self.input + self.cache_read + self.cache_write + self.cache_write_1h

    def cache_hit_rate(self) -> float | None:
        prompt = self.prompt_tokens()
        return (self.cache_read / prompt) if prompt else None


@dataclass
class SessionAgg:
    harness: str
    path: str
    session_id: str = ""
    cwd: str = ""
    first_ts: str = ""
    last_ts: str = ""
    by_model: dict[str, Bucket] = field(default_factory=dict)
    compactions: list[dict] = field(default_factory=list)   # {ts, tokens_before}
    # Claude Code writes one jsonl line per content block of a response, each
    # repeating the same usage. This maps an API call to what was already
    # billed for it so a repeat replaces rather than adds.
    claude_calls: dict = field(default_factory=dict)

    def bucket(self, model: str) -> Bucket:
        return self.by_model.setdefault(model or "<unknown>", Bucket())

    def combined(self) -> Bucket:
        agg = Bucket()
        for b in self.by_model.values():
            agg.add(b)
        return agg

    def total_cost(self, prices: dict) -> float:
        return sum((b.cost(m, prices) or 0.0) for m, b in self.by_model.items())

    def unpriced(self, prices: dict) -> list[str]:
        return [m for m, b in self.by_model.items()
                if price_for(m, prices) is None and b.total_tokens() > 0]

    def cost_parts(self, prices: dict) -> dict:
        """Per-component spend summed across models, each at its own rate."""
        agg = {"input": 0.0, "output": 0.0, "cache_read": 0.0,
               "cache_write": 0.0, "cache_write_1h": 0.0,
               "uncached_equivalent": 0.0}
        for model, bucket in self.by_model.items():
            parts = bucket.cost_parts(model, prices)
            if parts is None:
                continue
            for key in agg:
                agg[key] += parts[key]
        return agg

    def cache_savings(self, prices: dict) -> float:
        """What the cache reads would have cost as ordinary input, minus what they did
        cost. Positive means caching reduced this bill."""
        parts = self.cost_parts(prices)
        return parts["uncached_equivalent"] - parts["cache_read"]


# =============================================================================
# Harness plug-ins
#
# Each plug-in provides:
#   name       - short id
#   roots      - list of glob patterns under $HOME to search for JSONL logs
#   parse(line, agg) - update `agg` with one JSONL line
# =============================================================================

def _int(v) -> int:
    """Best-effort int coercion. Non-numeric / NaN / None -> 0.
    Untrusted JSON can carry anything; the parser must never crash."""
    if v is None or v is False:
        return 0
    if isinstance(v, bool):        # True already caught by False check above
        return int(v)
    if isinstance(v, int):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    if f != f or f == float("inf") or f == float("-inf"):  # NaN / inf
        return 0
    return int(f)


def _ts_update(agg: SessionAgg, ts: str) -> None:
    if not ts:
        return
    if not agg.first_ts or ts < agg.first_ts:
        agg.first_ts = ts
    if ts > agg.last_ts:
        agg.last_ts = ts


def parse_pi(line: str, agg: SessionAgg) -> None:
    try:
        d = json.loads(line)
    except Exception:
        return
    t = d.get("type")
    if t == "session":
        agg.session_id = d.get("id", agg.session_id)
        agg.cwd = d.get("cwd", agg.cwd)
        _ts_update(agg, d.get("timestamp", ""))
        return
    if t == "compaction":
        # Compaction has no `usage` block -- it's a checkpoint, not a billed
        # API call. The pre-compaction turns already contributed their real
        # tokens; the first post-compaction turn will re-pay for context
        # warming via its own cacheWrite. We just record the event so users
        # can see it in the report.
        agg.compactions.append({
            "ts":            d.get("timestamp", ""),
            "tokens_before": _int(d.get("tokensBefore")),
            "from_hook":     bool(d.get("fromHook", False)),
        })
        _ts_update(agg, d.get("timestamp", ""))
        return
    msg = d.get("message") or {}
    u = msg.get("usage") or d.get("usage")
    if not u:
        return
    model = msg.get("model") or d.get("model") or "<unknown>"
    b = agg.bucket(model)
    b.input       += _int(u.get("input"))
    b.output      += _int(u.get("output"))
    b.cache_read  += _int(u.get("cacheRead"))
    b.cache_write += _int(u.get("cacheWrite"))
    _ts_update(agg, d.get("timestamp", ""))


def claude_usage_tuple(u: dict) -> tuple[int, int, int, int, int]:
    """(input, output, cache_read, cache_write_5m, cache_write_1h) from a Claude
    Code `message.usage` object. Shared by the normal and run-report readers."""
    cc = u.get("cache_creation") or {}
    if cc:
        write_5m = _int(cc.get("ephemeral_5m_input_tokens"))
        write_1h = _int(cc.get("ephemeral_1h_input_tokens"))
    else:
        write_5m = _int(u.get("cache_creation_input_tokens"))
        write_1h = 0
    return (
        _int(u.get("input_tokens")),
        _int(u.get("output_tokens")),
        _int(u.get("cache_read_input_tokens")),
        write_5m,
        write_1h,
    )


def parse_claude_code(line: str, agg: SessionAgg) -> None:
    try:
        d = json.loads(line)
    except Exception:
        return
    if not agg.session_id:
        agg.session_id = d.get("sessionId", "")
    if not agg.cwd:
        agg.cwd = d.get("cwd", "")
    _ts_update(agg, d.get("timestamp", ""))
    # Claude Code marks the post-compaction summary message with
    # `isCompactSummary: true`. Record it (still let its usage bill normally,
    # because that turn *was* an API call that consumed tokens).
    if d.get("isCompactSummary") is True or d.get("type") == "compact_summary":
        agg.compactions.append({
            "ts":            d.get("timestamp", ""),
            "tokens_before": 0,        # Claude Code doesn't record this
            "from_hook":     False,
        })
    msg = d.get("message") or {}
    u = msg.get("usage")
    if not u:
        return
    model = msg.get("model") or "<unknown>"
    if model == "<synthetic>":
        return
    call = claude_usage_tuple(u)
    # Claude Code writes one jsonl line per content block of a response and
    # repeats the same usage on each. A streaming assistant message also
    # reports a growing output_tokens as the answer is produced. Both cases
    # mean the same API call, so the last line for a call replaces what the
    # earlier lines billed rather than adding to it.
    key = (msg.get("id"), d.get("requestId"))
    if key in agg.claude_calls:
        prev_model, prev = agg.claude_calls[key]
        pb = agg.bucket(prev_model)
        pb.input      -= prev[0]
        pb.output     -= prev[1]
        pb.cache_read -= prev[2]
        pb.cache_write    -= prev[3]
        pb.cache_write_1h -= prev[4]
    agg.claude_calls[key] = (model, call)
    b = agg.bucket(model)
    b.input      += call[0]
    b.output     += call[1]
    b.cache_read += call[2]
    b.cache_write    += call[3]
    b.cache_write_1h += call[4]


def parse_codex(line: str, agg: SessionAgg) -> None:
    """OpenAI Codex CLI — JSONL with `type=turn` entries carrying `usage`."""
    try:
        d = json.loads(line)
    except Exception:
        return
    _ts_update(agg, d.get("timestamp", "") or d.get("time", ""))
    if not agg.session_id:
        agg.session_id = d.get("session_id", "") or d.get("id", "")
    if not agg.cwd:
        agg.cwd = d.get("cwd", "")
    u = d.get("usage") or (d.get("response") or {}).get("usage")
    if not u:
        return
    model = d.get("model") or (d.get("response") or {}).get("model") or "<unknown>"
    b = agg.bucket(model)
    b.input       += _int(u.get("input_tokens",  u.get("prompt_tokens")))
    b.output      += _int(u.get("output_tokens", u.get("completion_tokens")))
    # OpenAI cached prompt tokens (Sept-2024+): usage.prompt_tokens_details.cached_tokens
    det = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    b.cache_read  += _int(det.get("cached_tokens"))


def parse_opencode(line: str, agg: SessionAgg) -> None:
    """SST opencode — JSONL with `tokens` sub-object on `assistant` messages."""
    try:
        d = json.loads(line)
    except Exception:
        return
    _ts_update(agg, d.get("time", {}).get("created", "") if isinstance(d.get("time"), dict) else d.get("time", ""))
    if not agg.session_id:
        agg.session_id = d.get("sessionID", "") or d.get("id", "")
    tok = d.get("tokens") or {}
    if not tok:
        return
    model = d.get("modelID") or d.get("model") or "<unknown>"
    b = agg.bucket(model)
    b.input       += _int(tok.get("input"))
    b.output      += _int(tok.get("output"))
    cache = tok.get("cache") or {}
    b.cache_read  += _int(cache.get("read"))
    b.cache_write += _int(cache.get("write"))


def _epoch_iso(value) -> str:
    """Convert a Unix timestamp to the ISO form used by the JSONL parsers."""
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return ""


def load_hermes_db(path: str) -> list[SessionAgg]:
    """Load Hermes token metadata without reading message content.

    The aggregate session row is authoritative. Positive residuals not yet
    represented in the per-model table are assigned to the session's recorded
    model, matching Hermes Insights' reconciliation behavior.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "sessions" not in tables:
            return []

        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        required = {
            "id", "model", "started_at", "ended_at", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens", "cwd",
        }
        if not required.issubset(session_columns):
            return []

        parent_expr = (
            "parent_session_id"
            if "parent_session_id" in session_columns
            else "NULL AS parent_session_id"
        )
        end_reason_expr = (
            "end_reason" if "end_reason" in session_columns else "NULL AS end_reason"
        )
        rows = conn.execute(
            f"""SELECT id, model, started_at, ended_at, cwd,
                       input_tokens, output_tokens, cache_read_tokens,
                       cache_write_tokens, {parent_expr}, {end_reason_expr}
                  FROM sessions"""
        ).fetchall()
        aggs: dict[str, SessionAgg] = {}
        aggregate: dict[str, Bucket] = {}
        attributed: dict[str, Bucket] = {}
        by_id = {}
        for row in rows:
            sid = str(row["id"] or "")
            if not sid:
                continue
            agg = SessionAgg(harness="hermes", path=path, session_id=sid)
            agg.cwd = str(row["cwd"] or "")
            agg.first_ts = _epoch_iso(row["started_at"])
            agg.last_ts = _epoch_iso(row["ended_at"] or row["started_at"])
            aggs[sid] = agg
            by_id[sid] = row
            aggregate[sid] = Bucket(
                input=_int(row["input_tokens"]),
                output=_int(row["output_tokens"]),
                cache_read=_int(row["cache_read_tokens"]),
                cache_write=_int(row["cache_write_tokens"]),
            )
            attributed[sid] = Bucket()

        if "session_model_usage" in tables:
            usage_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(session_model_usage)")
            }
            usage_required = {
                "session_id", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "last_seen",
            }
            if usage_required.issubset(usage_columns):
                usage_rows = conn.execute(
                    """SELECT session_id, model, input_tokens, output_tokens,
                              cache_read_tokens, cache_write_tokens, last_seen
                         FROM session_model_usage"""
                ).fetchall()
                for row in usage_rows:
                    sid = str(row["session_id"] or "")
                    agg = aggs.get(sid)
                    if agg is None:
                        continue
                    model = str(row["model"] or "<unknown>")
                    delta = Bucket(
                        input=_int(row["input_tokens"]),
                        output=_int(row["output_tokens"]),
                        cache_read=_int(row["cache_read_tokens"]),
                        cache_write=_int(row["cache_write_tokens"]),
                    )
                    agg.bucket(model).add(delta)
                    attributed[sid].add(delta)
                    _ts_update(agg, _epoch_iso(row["last_seen"]))

        for sid, agg in aggs.items():
            total = aggregate[sid]
            used = attributed[sid]
            residual = Bucket(
                input=max(0, total.input - used.input),
                output=max(0, total.output - used.output),
                cache_read=max(0, total.cache_read - used.cache_read),
                cache_write=max(0, total.cache_write - used.cache_write),
            )
            if residual.total_tokens() > 0:
                model = str(by_id[sid]["model"] or "<unknown>")
                agg.bucket(model).add(residual)

        # Legacy rotating compression created a continuation session. Merge only
        # when the parent explicitly ended because of compression; ordinary
        # branches and delegated children remain independent billable sessions.
        ordered_ids = sorted(
            aggs,
            key=lambda sid: aggs[sid].first_ts or "",
        )
        for sid in ordered_ids:
            child = aggs.get(sid)
            if child is None:
                continue
            parent_id = str(by_id[sid]["parent_session_id"] or "")
            parent = aggs.get(parent_id)
            if parent is None or str(by_id[parent_id]["end_reason"] or "") != "compression":
                continue
            before = parent.combined().total_tokens()
            for model, bucket in parent.by_model.items():
                child.bucket(model).add(bucket)
            if parent.first_ts and (
                not child.first_ts or parent.first_ts < child.first_ts
            ):
                child.first_ts = parent.first_ts
            child.compactions = parent.compactions + [
                {"ts": parent.last_ts, "tokens_before": before}
            ] + child.compactions
            del aggs[parent_id]

        return sorted(aggs.values(), key=lambda a: a.last_ts or "", reverse=True)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


HARNESSES = {
    "pi": {
        "roots": [".pi/agent/sessions"],
        "parse": parse_pi,
    },
    "claude-code": {
        "roots": [".claude/projects"],
        "parse": parse_claude_code,
    },
    "codex": {
        "roots": [".codex/sessions", ".codex/history"],
        "parse": parse_codex,
    },
    "opencode": {
        "roots": [".local/share/opencode", ".config/opencode"],
        "parse": parse_opencode,
    },
    "hermes": {
        "roots": [".hermes"],
        "parse": None,
    },
}


# =============================================================================
# Discovery / loading
# =============================================================================

def discover(harness: str) -> list[str]:
    home = Path.home()
    if harness == "hermes":
        if sys.platform == "win32":
            default_root = Path(os.environ.get("LOCALAPPDATA", home)) / "hermes"
        else:
            default_root = home / ".hermes"
        root = Path(os.environ.get("HERMES_HOME", default_root))
        db = root / "state.db"
        return [str(db)] if db.is_file() else []
    out: list[str] = []
    for rel in HARNESSES[harness]["roots"]:
        root = home / rel
        if not root.exists():
            continue
        out.extend(str(p) for p in root.rglob("*.jsonl"))
    return sorted(out)


def load_session(path: str, harness: str) -> SessionAgg:
    agg = SessionAgg(harness=harness, path=path)
    parse = HARNESSES[harness]["parse"]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s:
                    parse(s, agg)
    except OSError:
        pass
    if not agg.session_id:
        agg.session_id = Path(path).stem
    return agg


def load_sessions(path: str, harness: str) -> list[SessionAgg]:
    """Load every logical session stored in one harness artifact."""
    if harness == "hermes":
        return load_hermes_db(path)
    return [load_session(path, harness)]


def artifact_activity(path: str, harness: str) -> float:
    """Read a bounded JSONL tail and return its latest logical timestamp."""
    parse = HARNESSES[harness]["parse"]
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - 131_072))
            tail = f.read()
        agg = SessionAgg(harness=harness, path=path)
        for raw in reversed(tail.splitlines()):
            parse(raw.decode("utf-8", errors="replace"), agg)
            if agg.last_ts:
                dt = as_dt(agg.last_ts)
                if dt:
                    return dt.timestamp()
    except OSError:
        pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# =============================================================================
# Presentation
# =============================================================================

def money(x: float) -> str:
    return f"${x:,.4f}"

def toks(n: int) -> str:
    return f"{n:,}"

def norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))

def parse_since(s: str) -> datetime:
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        print(f"warning: bad --since {s!r}", file=sys.stderr)
        return datetime.min.replace(tzinfo=timezone.utc)

def as_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def print_session(a: SessionAgg, prices: dict, by_model: bool) -> None:
    c = a.combined()
    print(f"-- {a.harness}  {a.session_id}")
    if a.cwd:
        print(f"   cwd:    {a.cwd}")
    if a.first_ts or a.last_ts:
        print(f"   time:   {a.first_ts}  ->  {a.last_ts}")
    print(f"   file:   {a.path}")
    print(f"   tokens: in={toks(c.input)} out={toks(c.output)} "
          f"cache_r={toks(c.cache_read)} cache_w={toks(c.cache_write)} "
          f"cache_w_1h={toks(c.cache_write_1h)} "
          f"total={toks(c.total_tokens())}")
    if a.compactions:
        tb = [x["tokens_before"] for x in a.compactions if x["tokens_before"]]
        detail = f" (tokens_before: {', '.join(toks(t) for t in tb)})" if tb else ""
        print(f"   compact: {len(a.compactions)} event(s){detail}")
    # Cache reads are the bulk of a long agent session and are billed at a fraction of
    # the input rate, so a total that looks alarming is usually volume, not rate. Show
    # the hit rate and the counterfactual so that is checkable rather than asserted.
    hit = c.cache_hit_rate()
    if hit is not None and c.cache_read:
        saved = a.cache_savings(prices)
        print(f"   cache:  {hit * 100:.1f}% of {toks(c.prompt_tokens())} prompt tokens "
              f"served from cache · saved {money(saved)} vs the same tokens as fresh input")
    parts = a.cost_parts(prices)
    if any(parts[k] for k in ("input", "output", "cache_read", "cache_write", "cache_write_1h")):
        print(f"   spend:  in {money(parts['input'])} · out {money(parts['output'])} "
              f"· cache read {money(parts['cache_read'])} "
              f"· cache write {money(parts['cache_write'])}"
              + (f" · cache write 1h {money(parts['cache_write_1h'])}"
                 if parts["cache_write_1h"] else ""))
    unpriced = a.unpriced(prices)
    line = f"   cost:   {money(a.total_cost(prices))}"
    if unpriced:
        line += f"   (unpriced: {', '.join(unpriced)})"
    print(line)
    if by_model and len(a.by_model) > 1:
        for m, b in sorted(a.by_model.items()):
            cst = b.cost(m, prices)
            cs = money(cst) if cst is not None else "n/a"
            print(f"     - {m:40s} in={toks(b.input):>10s} "
                  f"out={toks(b.output):>10s} "
                  f"cache_r={toks(b.cache_read):>10s} "
                  f"cache_w={toks(b.cache_write):>10s} "
                  f"cache_w_1h={toks(b.cache_write_1h):>10s} cost={cs}")


def to_dict(a: SessionAgg, prices: dict) -> dict:
    c = a.combined()
    return {
        "harness":    a.harness,
        "session_id": a.session_id,
        "path":       a.path,
        "cwd":        a.cwd,
        "first_ts":   a.first_ts,
        "last_ts":    a.last_ts,
        "totals": {
            "input":      c.input,
            "output":     c.output,
            "cache_read": c.cache_read,
            "cache_write": c.cache_write,
            "cache_write_1h": c.cache_write_1h,
            "total":      c.total_tokens(),
            "prompt_tokens":  c.prompt_tokens(),
            "cache_hit_rate": (round(c.cache_hit_rate(), 6)
                               if c.cache_hit_rate() is not None else None),
            "cost_usd":   round(a.total_cost(prices), 6),
        },
        "spend_usd": {k: round(v, 6) for k, v in a.cost_parts(prices).items()},
        "cache_savings_usd": round(a.cache_savings(prices), 6),
        "compactions": a.compactions,
        "by_model": {
            m: {
                "input":      b.input,
                "output":     b.output,
                "cache_read": b.cache_read,
                "cache_write": b.cache_write,
                "cache_write_1h": b.cache_write_1h,
                "cost_usd":   (round(b.cost(m, prices), 6)
                               if b.cost(m, prices) is not None else None),
                "priced":     price_for(m, prices) is not None,
            } for m, b in a.by_model.items()
        },
        "unpriced_models": a.unpriced(prices),
    }


# =============================================================================
# Run report (Claude Code main session + its subagents)
# =============================================================================

TOOL_VERSION = "1.2.0"
RUN_REPORT_SCHEMA = "skill-cost-run-report-v1"
RUN_DISCLAIMER = (
    "USD figures are public-rate equivalents: measured tokens priced at the LiteLLM "
    "catalog rate. They are estimates, not a bill. Claude Code Pro/Max subscription usage "
    "is not billed per token, so these figures are not an additional API charge."
)
BILLED_NOTE = (
    "The actual billed figure, when present, is user-supplied through --billed-usd and is "
    "not verified by this tool. It is not derived from the logs."
)
UNATTRIBUTED = "unattributed"


def claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _norm_agent_id(key: str) -> str:
    """Accept `agent-<id>`, `<id>`, `<id>.jsonl` or a session id; return the bare id."""
    s = key.strip()
    if s.startswith("agent-"):
        s = s[len("agent-"):]
    if s.endswith(".jsonl"):
        s = s[: -len(".jsonl")]
    return s


def load_role_map(path: str) -> tuple[dict, str]:
    """Read a role map: a JSON object of {session-id | agent-id: role}. Keys starting
    with `_` are comments and ignored. Returns (map, sha256 of the file bytes)."""
    raw = Path(path).read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("role map must be a JSON object of {id: role}")
    out: dict[str, str] = {}
    for key, role in data.items():
        if str(key).startswith("_"):
            continue
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"role for {key!r} must be a non-empty string")
        out[_norm_agent_id(str(key))] = role.strip()
    return out, hashlib.sha256(raw).hexdigest()


_ABSENT = object()
_BAD = object()


def _count_field(src: dict, key: str):
    """One token count from untrusted JSON. Absent -> _ABSENT. Negative, boolean,
    non-numeric, non-integral or non-finite -> _BAD. Used by run reports only: the
    default view keeps claude_usage_tuple, where True would count as 1 and -3 as -3."""
    if key not in src:
        return _ABSENT
    v = src[key]
    if isinstance(v, bool):
        return _BAD
    if isinstance(v, int):
        return v if v >= 0 else _BAD
    if isinstance(v, float) and math.isfinite(v) and v.is_integer() and v >= 0:
        return int(v)
    return _BAD


def run_usage_tuple(u: dict):
    """Strict (input, output, cache_read, cache_write_5m, cache_write_1h) from a Claude
    Code `message.usage` object, as (tuple, split_inferred). Returns None when the record
    is malformed, and the caller excludes it from totals. Never zero-fills a count that
    cannot be placed: a missing 5m or 1h part is derived from cache_creation_input_tokens,
    and if that total is absent too the record is malformed."""
    fixed = []
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
        v = _count_field(u, key)
        if v is _BAD:
            return None
        fixed.append(0 if v is _ABSENT else v)
    total = _count_field(u, "cache_creation_input_tokens")
    if total is _BAD:
        return None
    cc = u.get("cache_creation")
    legacy = cc is None          # older records carry only the total
    if legacy:
        cc = {}
    elif not isinstance(cc, dict):
        return None
    five = _count_field(cc, "ephemeral_5m_input_tokens")
    one = _count_field(cc, "ephemeral_1h_input_tokens")
    if five is _BAD or one is _BAD:
        return None
    inferred = False
    if five is _ABSENT and one is _ABSENT:
        if total is _ABSENT:
            write_5m = write_1h = 0
        else:
            write_5m, write_1h = total, 0
            inferred = not legacy
    elif five is _ABSENT or one is _ABSENT:
        if total is _ABSENT:
            return None
        if five is _ABSENT:
            if one > total:
                return None
            write_5m, write_1h = total - one, one
        else:
            if five > total:
                return None
            write_5m, write_1h = five, total - five
        inferred = True
    else:
        write_5m, write_1h = five, one
    return (fixed[0], fixed[1], fixed[2], write_5m, write_1h), inferred


@dataclass
class LogRead:
    """One Claude Code transcript, read whole. `usage` holds one entry per usage-bearing
    line; deduplication happens later, across every file of the run."""
    path: Path
    kind: str                        # "main" | "subagent"
    agent_id: str = ""
    session_id: str = ""
    cwd: str = ""
    sha256: str = ""
    size: int = 0
    mtime: str = ""
    first_ts: str = ""
    last_ts: str = ""
    usage: list = field(default_factory=list)
    advisor_models: set = field(default_factory=set)
    bad_lines: int = 0
    malformed_usage: int = 0
    # agentId values named by toolUseResult records: subagents this transcript ran.
    referenced_agents: set = field(default_factory=set)
    error: str = ""
    agent_type: str = ""


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_claude_log(path: Path, kind: str, agent_id: str = "") -> LogRead:
    r = LogRead(path=path, kind=kind, agent_id=agent_id)
    try:
        data = path.read_bytes()
        st = path.stat()
    except OSError as e:
        r.error = _error_text(e)
        return r
    r.sha256 = hashlib.sha256(data).hexdigest()
    r.size = len(data)
    r.mtime = _iso_from_epoch(st.st_mtime)
    for raw in data.splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            d = json.loads(s.decode("utf-8"))
        except (ValueError, RecursionError):      # UnicodeDecodeError is a ValueError
            r.bad_lines += 1
            continue
        if not isinstance(d, dict):
            r.bad_lines += 1
            continue
        if not r.session_id:
            r.session_id = str(d.get("sessionId") or "")
        if not r.cwd:
            r.cwd = str(d.get("cwd") or "")
        ts = d.get("timestamp")
        if isinstance(ts, str) and ts:
            if not r.first_ts or ts < r.first_ts:
                r.first_ts = ts
            if ts > r.last_ts:
                r.last_ts = ts
        if d.get("advisorModel"):
            r.advisor_models.add(str(d["advisorModel"]))
        tool_result = d.get("toolUseResult")
        if isinstance(tool_result, dict) and isinstance(tool_result.get("agentId"), str):
            if tool_result["agentId"]:
                r.referenced_agents.add(tool_result["agentId"])
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        u = msg.get("usage")
        if u is None or u == {}:
            continue
        model = str(msg.get("model") or "<unknown>")
        if model == "<synthetic>":
            continue
        parsed = run_usage_tuple(u) if isinstance(u, dict) else None
        if parsed is None:
            r.malformed_usage += 1
            continue
        tokens, split_inferred = parsed
        msg_id = msg.get("id")
        key = (str(msg_id), str(d.get("requestId") or "")) if msg_id else None
        r.usage.append({"key": key, "model": model, "tokens": tokens,
                        "split_inferred": split_inferred})
    return r


def _total(tokens: tuple) -> int:
    return sum(tokens)


def select_main_session(root: Path, session_sub: str | None, cwd: str | None) -> Path | None:
    """Main transcripts are `<projects>/<project>/<session>.jsonl`. Subagent transcripts
    live one level deeper and are never candidates here."""
    if not root.is_dir():
        return None
    cands = sorted(p for p in root.glob("*/*.jsonl") if p.is_file())
    if session_sub:
        sub = session_sub.lower()
        cands = [p for p in cands if sub in p.stem.lower()]
    if cwd:
        want = norm_path(cwd)
        cands = [p for p in cands if _first_cwd(p) and norm_path(_first_cwd(p)) == want]
    if not cands:
        return None
    return max(cands, key=_claude_activity)


def _claude_activity(path: Path) -> float:
    """Newest timestamp in a transcript's tail, read without the default parser. The
    default parser raises on a non-object line or a non-object usage, and a run report
    must not crash while choosing its session."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - 131_072))
            tail = f.read()
        for raw in reversed(tail.splitlines()):
            try:
                d = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                continue
            ts = d.get("timestamp") if isinstance(d, dict) else None
            if isinstance(ts, str) and ts:
                dt = as_dt(ts)
                if dt:
                    return dt.timestamp()
    except OSError:
        pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _first_cwd(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 200:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except Exception:
                    continue
                if cwd:
                    return str(cwd)
    except OSError:
        pass
    return ""


def _error_text(e: Exception) -> str:
    """Errno text or exception class only. str(e) on an OSError embeds the file path,
    and a run report must never carry absolute paths."""
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return type(e).__name__


def _read_meta_agent_type(path: Path) -> tuple[str, str]:
    """(agentType, error). Only agentType is kept; the sidecar holds other fields."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return str(d.get("agentType") or ""), ""
    except (OSError, ValueError, AttributeError) as e:
        return "", _error_text(e)


def _report_path(log: LogRead, main: LogRead) -> str:
    """Transcript path as written to a report. It never includes the encoded project
    directory, because that directory name spells out the absolute cwd. Main session:
    `<session>.jsonl`. Subagent: `<session>/subagents/<file>`."""
    if log.kind == "main":
        return log.path.name
    return f"{main.path.stem}/subagents/{log.path.name}"


def _cwd_identity(cwd: str) -> tuple[str, str | None]:
    """(basename, first 8 hex chars of sha256(cwd)). The basename is for reading; the
    digest lets two reports from the same directory be correlated without the path."""
    if not cwd:
        return "", None
    parts = [p for p in re.split(r"[\\/]+", cwd) if p]
    digest = hashlib.sha256(cwd.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return (parts[-1] if parts else "/"), digest


PRICE_ASSUMPTIONS = {
    "cache_read": "cache reads priced at 10% of the input rate; the catalog has no cache_read price",
    "cache_write": "5-minute cache writes priced at 125% of the input rate; the catalog has no "
                   "cache_write price",
    "cache_write_1h": "1-hour cache writes priced at 200% of the input rate; the catalog has no "
                      "cache_write_1h price",
    "cache_write_split": "cache writes without a full 5m/1h breakdown; the split was inferred "
                         "from cache_creation_input_tokens",
}
UNKNOWN_PRICE_REASONS = {
    "no-catalog-entry": "no exact catalog entry",
    "incomplete-price-entry": "catalog entry has no input or output price",
}


def _run_usd(b: Bucket, model: str, prices: dict) -> tuple[float | None, str, list]:
    """Public-rate USD for one run-report row: (usd, reason, gaps). usd is None when the
    model cannot be priced exactly, and reason says why. A catalog entry with no output
    price is never priced as if output were free. A missing cache price falls back to the
    multiplier in PRICE_ASSUMPTIONS, and gaps lists each (component, tokens) it touched.
    Unlike Bucket.cost, a catalog value of 0 is kept as 0."""
    p = exact_price_for(model, prices)
    if p is None:
        return None, "no-catalog-entry", []
    if p.get("input") is None or p.get("output") is None:
        return None, "incomplete-price-entry", []
    inp = float(p["input"])
    gaps: list[tuple[str, int]] = []

    def rate(field_name: str, multiple: float, tokens: int, component: str) -> float:
        v = p.get(field_name)
        if v is None:
            if tokens:
                gaps.append((component, tokens))
            return inp * multiple
        return float(v)

    cache_read = rate("cache_read", 0.10, b.cache_read, "cache_read")
    cache_write = rate("cache_write", 1.25, b.cache_write, "cache_write")
    cache_write_1h = rate("cache_write_1h", 2.0, b.cache_write_1h, "cache_write_1h")
    usd = (b.input * inp + b.output * float(p["output"]) + b.cache_read * cache_read
           + b.cache_write * cache_write + b.cache_write_1h * cache_write_1h)
    return usd, "", gaps


def build_run_report(main: LogRead, subs: list[LogRead], meta_only: list[str],
                     meta_errors: list[str], role_map: dict, prices: dict,
                     billed: tuple[float, str] | None,
                     prices_path: Path, role_map_info: dict) -> dict:
    """Assemble the run-level report. Measured tokens come from the logs; USD is a
    public-rate equivalent; actual billed spend is only ever the user-supplied figure."""
    inaccessible: list[dict] = []
    warnings: list[str] = []
    main_ids = {main.path.stem, main.session_id} - {""}
    sub_ids = {s.agent_id for s in subs}

    for agent_id in meta_only:
        inaccessible.append({"kind": "subagent", "id": agent_id,
                             "reason": "subagent metadata present but transcript file missing"})
    for s in subs:
        if s.error:
            inaccessible.append({"kind": "subagent", "id": s.agent_id,
                                 "reason": f"transcript unreadable: {s.error}"})
    # A subagent that a transcript says it ran (toolUseResult.agentId) must be in the
    # run. If no transcript exists for it, the totals are missing its calls.
    referenced = set().union(*(log.referenced_agents for log in [main] + subs))
    listed = {item["id"] for item in inaccessible}
    for agent_id in sorted(referenced - sub_ids - listed):
        inaccessible.append({"kind": "subagent", "id": agent_id,
                             "reason": "referenced by a toolUseResult.agentId in a transcript, "
                                       "but no subagent transcript was found"})
    for key in sorted(role_map):
        if key in main_ids or key in sub_ids or key in meta_only:
            continue
        inaccessible.append({"kind": "role-map-reference", "id": key,
                             "reason": "referenced in --role-map but no matching session "
                                       "or subagent transcript was found"})
    for err in meta_errors:
        warnings.append(f"subagent metadata unreadable (agentType unknown): {err}")

    # Global dedupe: one usage record per API call (message.id + requestId) across the
    # main transcript and every subagent transcript. Streaming records for the same call
    # carry a growing output count, so the largest total wins; ties keep the first seen.
    readable = [main] + [s for s in subs if not s.error]
    records = [(log, rec) for log in readable for rec in log.usage]
    # A message id that appears without a requestId anywhere in the run is one call:
    # every copy with that id merges, whether or not it carries a requestId. Ids that
    # only ever appear with distinct requestIds stay distinct, as before.
    id_only = {rec["key"][0] for _, rec in records if rec["key"] and not rec["key"][1]}
    keyed: dict = {}
    unkeyed: list[tuple[LogRead, dict]] = []
    counts = {"keyedRecords": 0, "duplicatesIgnored": 0, "unkeyedRecords": 0}
    for log, rec in records:
        if rec["key"] is None:
            unkeyed.append((log, rec))
            counts["unkeyedRecords"] += 1
            continue
        msg_id, req = rec["key"]
        key = (msg_id, "") if msg_id in id_only else (msg_id, req)
        counts["keyedRecords"] += 1
        prev = keyed.get(key)
        if prev is None:
            keyed[key] = (log, rec)
        else:
            counts["duplicatesIgnored"] += 1
            if _total(rec["tokens"]) > _total(prev[1]["tokens"]):
                keyed[key] = (log, rec)
    if counts["unkeyedRecords"]:
        warnings.append(f"{counts['unkeyedRecords']} usage record(s) had no message id and "
                        "could not be deduplicated; each was counted once")
    winners = list(keyed.values()) + unkeyed

    # Lead model: the main session's model with the most output tokens.
    main_out: dict[str, int] = {}
    for log, rec in winners:
        if log is main:
            main_out[rec["model"]] = main_out.get(rec["model"], 0) + rec["tokens"][1]
    lead_model = max(main_out, key=lambda m: (main_out[m], m)) if main_out else None

    def role_for(log: LogRead, model: str) -> str:
        if log is main:
            mapped = role_map.get(main.path.stem) or role_map.get(main.session_id)
            if mapped:
                return mapped
            if model == "<unknown>":
                return UNATTRIBUTED
            return "lead" if model == lead_model else UNATTRIBUTED
        mapped = role_map.get(log.agent_id)
        if mapped:
            return mapped
        if model == "<unknown>":
            return UNATTRIBUTED
        # Family-level default only: builder vs verifier is never guessed without a map.
        return f"subagent:{model}"

    rows: dict[tuple[str, str], Bucket] = {}
    roles_for_model: dict[str, set] = {}
    measured = Bucket()
    for log, rec in winners:
        model = rec["model"]
        role = role_for(log, model)
        t = rec["tokens"]
        b = Bucket(input=t[0], output=t[1], cache_read=t[2],
                   cache_write=t[3], cache_write_1h=t[4])
        rows.setdefault((role, model), Bucket()).add(b)
        measured.add(b)
        roles_for_model.setdefault(model, set()).add(role)

    row_out = []
    by_role: dict[str, dict] = {}
    unknown: dict[str, dict] = {}
    assumed: dict[tuple[str, str], int] = {}
    priced_total = 0.0
    for (role, model) in sorted(rows):
        b = rows[(role, model)]
        usd, reason, gaps = _run_usd(b, model, prices)
        row_out.append({
            "role": role,
            "model": model,
            "priced": usd is not None,
            "tokens": _token_dict(b),
            "publicRateUsdEquivalent": round(usd, 6) if usd is not None else None,
        })
        slot = by_role.setdefault(role, {"tokens": Bucket(), "usd": 0.0, "unpricedTokens": 0})
        slot["tokens"].add(b)
        if usd is None:
            slot["unpricedTokens"] += b.total_tokens()
            info = unknown.setdefault(model, {"model": model, "tokens": 0, "roles": set(),
                                              "reason": reason})
            info["tokens"] += b.total_tokens()
            info["roles"].add(role)
        else:
            slot["usd"] += usd
            priced_total += usd
            for component, tokens in gaps:
                assumed[(model, component)] = assumed.get((model, component), 0) + tokens
    # A cache write whose 5m/1h split was inferred is priced on that inferred split.
    for log, rec in winners:
        if rec.get("split_inferred") and rec["model"] not in unknown:
            key = (rec["model"], "cache_write_split")
            assumed[key] = assumed.get(key, 0) + rec["tokens"][3] + rec["tokens"][4]

    if lead_model is None:
        warnings.append("no usage from the main session was found; no lead model inferred")
    advisor_seen = set()
    for log in readable:
        advisor_seen |= log.advisor_models
    for adv in sorted(advisor_seen - set(roles_for_model)):
        warnings.append(f"records declare advisorModel {adv!r} but no usage records for it "
                        "were found; any advisor tokens are not visible in these logs")

    incomplete = [f"{item['id']}: {item['reason']}" for item in inaccessible]
    for log in readable:
        if log.bad_lines:
            incomplete.append(f"{log.bad_lines} unparsable line(s) in {_report_path(log, main)}")
        if log.malformed_usage:
            incomplete.append(f"{log.malformed_usage} malformed usage record(s) (negative, "
                              f"boolean or non-numeric token counts, or a cache-write split "
                              f"that cannot be placed) excluded from totals in "
                              f"{_report_path(log, main)}")

    if billed is None:
        actual = {"usd": None, "reason": "not measured by this tool"}
    else:
        actual = {"usd": round(billed[0], 6), "source": billed[1],
                  "provenance": "user-supplied via --billed-usd/--billed-source; "
                                "not verified by this tool"}

    meta = prices.get("_meta") or {}
    prices_sha = hashlib.sha256(prices_path.read_bytes()).hexdigest() if prices_path.exists() else ""

    file_entries = []
    for log in [main] + subs:
        if log.error:
            continue
        entry = {
            "path": _report_path(log, main),
            "kind": log.kind,
            "sha256": log.sha256,
            "sizeBytes": log.size,
            "mtime": log.mtime,
            "usageRecords": len(log.usage),
            "malformedUsageRecords": log.malformed_usage,
            "unparsableLines": log.bad_lines,
        }
        if log.kind == "subagent":
            entry["agentId"] = log.agent_id
            entry["agentType"] = log.agent_type
        file_entries.append(entry)

    cwd_name, cwd_sha = _cwd_identity(main.cwd)
    return {
        "schema": RUN_REPORT_SCHEMA,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": {"name": "skill-cost", "version": TOOL_VERSION},
        "complete": not incomplete,
        "incompleteReasons": incomplete,
        "session": {
            "harness": "claude-code",
            "sessionId": main.session_id or main.path.stem,
            "cwd": cwd_name,
            "cwdSha256": cwd_sha,
            "firstTs": main.first_ts,
            "lastTs": main.last_ts,
            "leadModel": lead_model,
            "logPath": _report_path(main, main),
        },
        "files": file_entries,
        "inaccessible": inaccessible,
        "roleMap": role_map_info,
        "dedupe": counts,
        "malformedUsage": sum(log.malformed_usage for log in readable),
        "measuredTokens": _token_dict(measured),
        "publicRateUsdEquivalent": {
            "usd": round(priced_total, 6),
            "complete": not unknown,
            "kind": "public-rate equivalent, not a bill",
            "pricedRows": sum(1 for r in row_out if r["priced"]),
            "unpricedRows": sum(1 for r in row_out if not r["priced"]),
        },
        "unknownPrices": [
            {"model": m, "tokens": v["tokens"], "roles": sorted(v["roles"]),
             "reason": v["reason"]}
            for m, v in sorted(unknown.items())
        ],
        "priceAssumptions": [
            {"model": m, "component": c, "assumption": PRICE_ASSUMPTIONS[c], "tokens": n}
            for (m, c), n in sorted(assumed.items())
        ],
        "byRole": {
            role: {
                "tokens": _token_dict(v["tokens"]),
                "publicRateUsdEquivalent": round(v["usd"], 6),
                "unpricedTokens": v["unpricedTokens"],
            } for role, v in sorted(by_role.items())
        },
        "rows": row_out,
        "actualBilledSpend": actual,
        "disclaimer": RUN_DISCLAIMER + (" " + BILLED_NOTE if billed is not None else ""),
        "prices": {
            "file": prices_path.name,
            "sha256": prices_sha,
            "source": meta.get("source", "BerriAI/litellm"),
            "sourceUrl": meta.get("source_url", LITELLM_URL),
            "fetchedAt": meta.get("fetched_at"),
            "modelCount": meta.get("model_count"),
        },
        "warnings": warnings,
    }


def _token_dict(b: Bucket) -> dict:
    return {
        "input": b.input,
        "output": b.output,
        "cacheRead": b.cache_read,
        "cacheWrite": b.cache_write,
        "cacheWrite1h": b.cache_write_1h,
        "total": b.total_tokens(),
    }


def render_run_markdown(rep: dict) -> str:
    def esc(s: str) -> str:
        return str(s).replace("|", "\\|")

    s = rep["session"]
    pub = rep["publicRateUsdEquivalent"]
    out = ["## skill-cost run report", ""]
    if rep["complete"]:
        status = "complete (measured tokens)"
    else:
        status = "INCOMPLETE — " + "; ".join(rep["incompleteReasons"])
    if pub["complete"]:
        status += "; USD complete"
    else:
        status += f"; USD partial: {len(rep['unknownPrices'])} unpriced model(s)"
    out.append(f"- **Status:** {status}")
    cwd_txt = (f"`{s['cwd']}` (cwdSha256 `{s['cwdSha256']}`)" if s["cwd"] else "unknown")
    out.append(f"- **Session:** `{s['sessionId']}` · lead model `{s['leadModel'] or 'unknown'}` "
               f"· cwd {cwd_txt}")
    out.append(f"- **Window:** {s['firstTs'] or '?'} → {s['lastTs'] or '?'}")
    subs = [f for f in rep["files"] if f["kind"] == "subagent"]
    out.append(f"- **Logs read:** 1 main + {len(subs)} subagent transcript(s)")
    out.append("")
    out.append("| Role | Model | Input | Output | Cache read | Cache write (5m+1h) "
               "| Public-rate USD eq. | Priced |")
    out.append("|---|---|---:|---:|---:|---:|---:|:---:|")
    for r in rep["rows"]:
        t = r["tokens"]
        usd = f"${r['publicRateUsdEquivalent']:,.4f}" if r["priced"] else "n/a"
        out.append(f"| {esc(r['role'])} | {esc(r['model'])} | {toks(t['input'])} "
                   f"| {toks(t['output'])} | {toks(t['cacheRead'])} "
                   f"| {toks(t['cacheWrite'] + t['cacheWrite1h'])} | {usd} "
                   f"| {'yes' if r['priced'] else 'no'} |")
    m = rep["measuredTokens"]
    if pub["complete"]:
        total_usd = f"**${pub['usd']:,.4f}**"
    else:
        total_usd = f"≥ **${pub['usd']:,.4f}** (partial)"
    out.append(f"| **total** | | {toks(m['input'])} | {toks(m['output'])} "
               f"| {toks(m['cacheRead'])} | {toks(m['cacheWrite'] + m['cacheWrite1h'])} "
               f"| {total_usd} | |")
    out.append("")
    out.append(f"- **Public-rate USD equivalent:** ${pub['usd']:,.4f} "
               f"({pub['kind']}; complete={str(pub['complete']).lower()})")
    if rep["unknownPrices"]:
        parts = [f"`{u['model']}` ({toks(u['tokens'])} tokens, no USD: "
                 f"{UNKNOWN_PRICE_REASONS.get(u['reason'], u['reason'])})"
                 for u in rep["unknownPrices"]]
        out.append("- **Unknown prices (never substituted):** " + ", ".join(parts)
                   + " — run `python3 cost.py --refresh-prices` if the model is new")
    if rep["priceAssumptions"]:
        parts = [f"`{a['model']}`, {toks(a['tokens'])} tokens: {a['assumption']}"
                 for a in rep["priceAssumptions"]]
        out.append("- **Price assumptions (USD only):** " + "; ".join(parts))
    b = rep["actualBilledSpend"]
    if b["usd"] is None:
        out.append(f"- **User-supplied billed figure (unverified):** none supplied ({b['reason']})")
    else:
        out.append(f"- **User-supplied billed figure (unverified):** ${b['usd']:,.4f} "
                   f"(source: {b['source']}; {b['provenance']})")
    if rep["inaccessible"]:
        out.append("- **Inaccessible:**")
        for item in rep["inaccessible"]:
            out.append(f"  - {item['kind']} `{item['id']}`: {item['reason']}")
    for w in rep["warnings"]:
        out.append(f"- Warning: {w}")
    out.append("")
    out.append(f"> {rep['disclaimer']}")
    out.append("")
    p = rep["prices"]
    out.append(f"Prices: {p['modelCount']} models from {p['source']} (fetched {p['fetchedAt']}), "
               f"prices.json sha256 `{p['sha256'][:12]}`. skill-cost {rep['tool']['version']}, "
               f"generated {rep['generatedAt']}.")
    return "\n".join(out) + "\n"


def run_report_cmd(args: argparse.Namespace, prices: dict) -> int:
    if args.harness not in ("all", "claude-code"):
        print("error: --run-report reads Claude Code sessions; use --harness claude-code "
              "or omit --harness", file=sys.stderr)
        return 2
    if args.json and args.markdown:
        print("error: choose one of --json or --markdown", file=sys.stderr)
        return 2
    if (args.billed_usd is None) != (args.billed_source is None):
        print("error: --billed-usd and --billed-source must be given together", file=sys.stderr)
        return 2
    billed = None
    if args.billed_usd is not None:
        if not math.isfinite(args.billed_usd) or args.billed_usd < 0:
            print("error: --billed-usd must be a finite, non-negative number", file=sys.stderr)
            return 2
        if not args.billed_source.strip():
            print("error: --billed-source must not be empty", file=sys.stderr)
            return 2
        billed = (args.billed_usd, args.billed_source.strip())

    role_map: dict = {}
    role_map_info: dict = {"provided": False}
    if args.role_map:
        # Static text only: OSError and json messages can carry the absolute path.
        try:
            role_map, map_sha = load_role_map(args.role_map)
        except OSError as e:
            print(f"error: --role-map: cannot read the role map file ({_error_text(e)})",
                  file=sys.stderr)
            return 2
        except ValueError:
            print("error: --role-map: the role map must be a JSON object of {id: role} "
                  "with non-empty string roles", file=sys.stderr)
            return 2
        role_map_info = {"provided": True, "file": Path(args.role_map).name,
                         "sha256": map_sha, "entries": len(role_map)}

    root = claude_projects_root()
    main_path = select_main_session(root, args.session, args.cwd)
    if main_path is None:
        print("error: no matching Claude Code session in the projects directory",
              file=sys.stderr)
        return 1
    main = read_claude_log(main_path, "main")
    if main.error:
        print(f"error: cannot read main session transcript: {main.error}", file=sys.stderr)
        return 1

    sub_dir = main_path.with_suffix("") / "subagents"
    subs: list[LogRead] = []
    meta_only: list[str] = []
    meta_errors: list[str] = []
    if sub_dir.is_dir():
        sub_ids = set()
        for path in sorted(sub_dir.glob("agent-*.jsonl")):
            agent_id = path.stem[len("agent-"):]
            sub_ids.add(agent_id)
            log = read_claude_log(path, "subagent", agent_id)
            meta = sub_dir / f"{path.stem}.meta.json"
            if meta.is_file():
                log.agent_type, err = _read_meta_agent_type(meta)
                if err:
                    meta_errors.append(f"{agent_id}: {err}")
            subs.append(log)
        for meta in sorted(sub_dir.glob("agent-*.meta.json")):
            agent_id = meta.name[len("agent-"):-len(".meta.json")]
            if agent_id not in sub_ids:
                meta_only.append(agent_id)

    rep = build_run_report(main, subs, meta_only, meta_errors, role_map, prices, billed,
                           PRICES_JSON, role_map_info)
    if args.markdown:
        sys.stdout.write(render_run_markdown(rep))
    else:
        print(json.dumps(rep, indent=2))
    if not rep["complete"]:
        print("warning: run report is incomplete; see incompleteReasons", file=sys.stderr)
        if args.strict:
            return 3
    return 0


# =============================================================================
# Main
# =============================================================================

def run_report_entry(args: argparse.Namespace) -> int:
    """Entry point for --run-report. Nothing reaches stderr as a traceback: an unexpected
    exception is reported as one line that names its class only, never its message."""
    try:
        if args.refresh_prices:
            refresh_prices(write=True, quiet=True)
            print("prices.json refreshed", file=sys.stderr)
            return 0
        prices = load_prices(offline=args.offline, redact=True)
        return run_report_cmd(args, prices)
    except Exception as e:
        print(f"error: run-report failed ({type(e).__name__})", file=sys.stderr)
        return 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Compute token-usage cost from agent harness session logs.",
    )
    ap.add_argument("--current", action="store_true",
                    help="[default] the newest logical session across selected harnesses")
    ap.add_argument("--latest", action="store_true",
                    help="latest session per harness in the current cwd")
    ap.add_argument("--all", action="store_true", help="every session across every harness")
    ap.add_argument("--today", action="store_true", help="sessions with activity today (UTC)")
    ap.add_argument("--since", metavar="DATE", help="YYYY-MM-DD or ISO timestamp")
    ap.add_argument("--harness", choices=list(HARNESSES) + ["all"], default="all")
    ap.add_argument("--cwd", metavar="PATH", help="filter by session cwd")
    ap.add_argument("--session", metavar="ID", help="substring match on session id")
    ap.add_argument("--by-model", action="store_true", help="per-model breakdown")
    ap.add_argument("--list", action="store_true", help="one line per session")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--refresh-prices", action="store_true",
                    help="force-fetch the latest prices from LiteLLM and update prices.json")
    ap.add_argument("--offline", action="store_true",
                    help="skip any network access; use bundled prices.json as-is")
    ap.add_argument("--run-report", action="store_true",
                    help="run-level report for one Claude Code session and its subagents, "
                         "by role (see SKILL.md); with --json or --markdown")
    ap.add_argument("--markdown", action="store_true",
                    help="with --run-report: render the report as markdown")
    ap.add_argument("--role-map", metavar="FILE",
                    help="with --run-report: JSON {session-id | agent-id: role}")
    ap.add_argument("--strict", action="store_true",
                    help="with --run-report: exit 3 when the report is incomplete")
    ap.add_argument("--billed-usd", metavar="N", type=float,
                    help="with --run-report: user-supplied billed spend in USD; echoed as "
                         "unverified, with its source")
    ap.add_argument("--billed-source", metavar="TEXT",
                    help="with --run-report: where --billed-usd came from (required with it)")
    args = ap.parse_args(argv)

    run_only = [flag for flag, given in (
        ("--billed-usd", args.billed_usd is not None),
        ("--billed-source", args.billed_source is not None),
        ("--markdown", args.markdown),
        ("--role-map", args.role_map is not None),
        ("--strict", args.strict),
    ) if given]
    if run_only and not args.run_report:
        verb = "is" if len(run_only) == 1 else "are"
        print(f"error: {', '.join(run_only)} {verb} only valid with --run-report",
              file=sys.stderr)
        return 2

    if args.run_report:
        return run_report_entry(args)

    if args.refresh_prices:
        refresh_prices(write=True)
        return 0

    prices = load_prices(offline=args.offline)

    # Default view = --current
    if not (args.all or args.today or args.since or args.session
            or args.cwd or args.current or args.latest):
        args.current = True

    # Collect candidate files
    harnesses = list(HARNESSES) if args.harness == "all" else [args.harness]
    files: list[tuple[str, str]] = []

    if args.current:
        # Hermes DBs contain many logical sessions, so load each active DB.
        # JSONL artifacts hold one session each; inspect only a bounded tail to
        # choose the newest logical session before fully parsing one file.
        all_files = [(h, p) for h in harnesses for p in discover(h)]
        if not all_files:
            print("no session files found", file=sys.stderr)
            return 1
        hermes_files = [(h, p) for h, p in all_files if h == "hermes"]
        jsonl_files = [(h, p) for h, p in all_files if h != "hermes"]
        files = hermes_files
        if jsonl_files:
            files.append(
                max(jsonl_files, key=lambda hp: artifact_activity(hp[1], hp[0]))
            )
    else:
        for h in harnesses:
            files += [(h, p) for p in discover(h)]

    if not files:
        print("no session files found", file=sys.stderr)
        return 1

    sessions = [
        session
        for h, p in files
        for session in load_sessions(p, h)
    ]

    # Filters
    if args.cwd:
        t = norm_path(args.cwd)
        sessions = [s for s in sessions if s.cwd and norm_path(s.cwd) == t]
    if args.session:
        sub = args.session.lower()
        sessions = [s for s in sessions if sub in s.session_id.lower()]
    if args.today:
        today = datetime.now(timezone.utc).date()
        sessions = [s for s in sessions
                    if (dt := as_dt(s.last_ts)) and dt.date() == today]
    if args.since:
        lo = parse_since(args.since)
        sessions = [s for s in sessions
                    if (dt := as_dt(s.last_ts)) and dt >= lo]
    if args.latest:
        cwd = norm_path(os.getcwd())
        pool = [s for s in sessions if s.cwd and norm_path(s.cwd) == cwd] or sessions
        pick: dict[str, SessionAgg] = {}
        for s in pool:
            cur = pick.get(s.harness)
            if cur is None or (s.last_ts or "") > (cur.last_ts or ""):
                pick[s.harness] = s
        sessions = list(pick.values())

    sessions.sort(key=lambda s: s.last_ts or "", reverse=True)
    if not (args.session or args.list):
        sessions = [s for s in sessions if s.combined().total_tokens() > 0]
    if args.current:
        sessions = sessions[:1]

    if args.json:
        meta = prices.get("_meta") or {}
        out = {
            "sessions": [to_dict(s, prices) for s in sessions],
            "grand_total_cost_usd": round(sum(s.total_cost(prices) for s in sessions), 6),
            "grand_total_tokens":   sum(s.combined().total_tokens() for s in sessions),
            "prices": {
                "file":         str(PRICES_JSON),
                "model_count":  meta.get("model_count", len(prices)),
                "source":       meta.get("source", "BerriAI/litellm"),
                "source_url":   meta.get("source_url", LITELLM_URL),
                "source_license": meta.get("source_license", "MIT"),
                "fetched_at":   meta.get("fetched_at"),
            },
            "powered_by": "gaia",
            "attribution": "Prices from BerriAI/litellm (MIT). "
                           "skill-cost by Gaia Research (https://gaiaskilltree.com).",
        }
        print(json.dumps(out, indent=2))
        return 0

    if args.list:
        print(f"{'harness':<12} {'session id':<40} {'tokens':>14} {'cost':>12} {'cmp':>4}  cwd")
        for s in sessions:
            b = s.combined()
            cmp_ = str(len(s.compactions)) if s.compactions else "-"
            print(f"{s.harness:<12} {s.session_id[:40]:<40} "
                  f"{toks(b.total_tokens()):>14} "
                  f"{money(s.total_cost(prices)):>12} {cmp_:>4}  {s.cwd}")
    else:
        for s in sessions:
            print_session(s, prices, args.by_model)
            print()

    tot_cost = sum(s.total_cost(prices) for s in sessions)
    tot_toks = sum(s.combined().total_tokens() for s in sessions)
    tot_cmp  = sum(len(s.compactions) for s in sessions)
    print("------------------------------------------------------")
    line = f"{len(sessions)} session(s)   tokens={toks(tot_toks)}   cost={money(tot_cost)}"
    if tot_cmp:
        line += f"   compactions={tot_cmp}"
    print(line)

    # The single most useful number for "why is this so much?": across everything shown,
    # how much of the prompt was cached, and what that saved against fresh input pricing.
    tot_prompt = sum(s.combined().prompt_tokens() for s in sessions)
    tot_cached = sum(s.combined().cache_read for s in sessions)
    tot_saved  = sum(s.cache_savings(prices) for s in sessions)
    if tot_cached:
        pct = tot_cached / tot_prompt * 100 if tot_prompt else 0.0
        print(f"cache: {pct:.1f}% of {toks(tot_prompt)} prompt tokens were cache hits   "
              f"saved={money(tot_saved)} vs uncached")

    all_unpriced = {m for s in sessions for m in s.unpriced(prices)}
    if all_unpriced:
        print(f"note: unpriced model(s): {', '.join(sorted(all_unpriced))}")
        print("      run  cost.py --refresh-prices  to pull the latest catalog.")

    # Attribution footer -- prices are not our data; credit the source.
    meta = prices.get("_meta") or {}
    fetched = meta.get("fetched_at", "unknown")
    n_models = meta.get("model_count", len(prices))
    print(f"prices: {n_models} models from LiteLLM "
          f"(github.com/BerriAI/litellm, MIT) -- fetched {fetched}")
    print("powered by gaia -- https://gaiaskilltree.com")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
