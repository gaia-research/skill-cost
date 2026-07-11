#!/usr/bin/env python3
"""
cost.py — Compute token-usage cost from agent harness JSONL session logs.

Supported harnesses (auto-detected):
  - pi           (~/.pi/agent/sessions/**/*.jsonl)
  - claude-code  (~/.claude/projects/**/*.jsonl)
  - codex        (~/.codex/sessions/**/*.jsonl)
  - opencode     (~/.local/share/opencode/**/*.jsonl)

Prices are read from `prices.json` shipped next to this script and refreshed
weekly from BerriAI/litellm's canonical model_prices_and_context_window.json
via GitHub Actions (see .github/workflows/update-prices.yml).

Zero dependencies. Python 3.8+, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


def load_prices(offline: bool = False) -> dict:
    """Load bundled prices.json. Silently refresh from LiteLLM if stale.

    Refresh is skipped when:
      - offline=True (--offline flag)
      - env SKILL_COST_NO_AUTO_REFRESH=1
      - the on-disk file is newer than SKILL_COST_MAX_AGE_DAYS (default 7)
    A network failure during auto-refresh is non-fatal: we keep using the
    cached file.
    """
    stale = _prices_age_days() > _max_age_days()
    if (stale
        and not offline
        and os.environ.get("SKILL_COST_NO_AUTO_REFRESH") not in ("1", "true", "yes")):
        try:
            return refresh_prices(write=True, quiet=True)
        except Exception as e:
            print(f"warning: auto-refresh of prices.json failed ({e}); "
                  f"using cached copy.", file=sys.stderr)

    if PRICES_JSON.exists():
        try:
            return json.loads(PRICES_JSON.read_text(encoding="utf-8"))
        except Exception as e:
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
        "note": "Pruned to input/output/cache_read/cache_write/provider. "
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


# =============================================================================
# Aggregation
# =============================================================================

@dataclass
class Bucket:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, o: "Bucket") -> None:
        self.input += o.input
        self.output += o.output
        self.cache_read += o.cache_read
        self.cache_write += o.cache_write

    def total_tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def cost(self, model: str, prices: dict) -> float | None:
        p = price_for(model, prices)
        if not p or p.get("input") is None:
            return None
        inp = float(p["input"])
        out = float(p.get("output") or 0)
        cr  = float(p.get("cache_read")  or inp * 0.10)
        cw  = float(p.get("cache_write") or inp * 1.25)
        return (self.input * inp
              + self.output * out
              + self.cache_read * cr
              + self.cache_write * cw)


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
    b = agg.bucket(model)
    b.input      += _int(u.get("input_tokens"))
    b.output     += _int(u.get("output_tokens"))
    b.cache_read += _int(u.get("cache_read_input_tokens"))
    cc = u.get("cache_creation") or {}
    if cc:
        b.cache_write += _int(cc.get("ephemeral_5m_input_tokens"))
        b.cache_write += _int(cc.get("ephemeral_1h_input_tokens"))
    else:
        b.cache_write += _int(u.get("cache_creation_input_tokens"))


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
}


# =============================================================================
# Discovery / loading
# =============================================================================

def discover(harness: str) -> list[str]:
    home = Path.home()
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
          f"total={toks(c.total_tokens())}")
    if a.compactions:
        tb = [x["tokens_before"] for x in a.compactions if x["tokens_before"]]
        detail = f" (tokens_before: {', '.join(toks(t) for t in tb)})" if tb else ""
        print(f"   compact: {len(a.compactions)} event(s){detail}")
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
                  f"cache_w={toks(b.cache_write):>10s} cost={cs}")


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
            "total":      c.total_tokens(),
            "cost_usd":   round(a.total_cost(prices), 6),
        },
        "compactions": a.compactions,
        "by_model": {
            m: {
                "input":      b.input,
                "output":     b.output,
                "cache_read": b.cache_read,
                "cache_write": b.cache_write,
                "cost_usd":   (round(b.cost(m, prices), 6)
                               if b.cost(m, prices) is not None else None),
                "priced":     price_for(m, prices) is not None,
            } for m, b in a.by_model.items()
        },
        "unpriced_models": a.unpriced(prices),
    }


# =============================================================================
# Main
# =============================================================================

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Compute token-usage cost from harness JSONL session logs.",
    )
    ap.add_argument("--current", action="store_true",
                    help="[default] the currently active session (newest-mtime JSONL)")
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
    args = ap.parse_args(argv)

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
        # Newest-mtime JSONL across the selected harnesses = the active session
        all_files = [(h, p) for h in harnesses for p in discover(h)]
        if not all_files:
            print("no session files found", file=sys.stderr)
            return 1
        newest = max(all_files, key=lambda hp: os.path.getmtime(hp[1]))
        files = [newest]
    else:
        for h in harnesses:
            files += [(h, p) for p in discover(h)]

    if not files:
        print("no session files found", file=sys.stderr)
        return 1

    sessions = [load_session(p, h) for h, p in files]

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
