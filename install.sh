#!/usr/bin/env bash
#
# install.sh — one-liner installer for the cost agent skill.
#
# Usage:
#   bash <(curl -sL https://raw.githubusercontent.com/gaia-research/skill-cost/main/install.sh)
#
# Auto-detects the agent skills directory and drops cost/ inside it.
# Ships with a current prices.json, and cost.py silently keeps it fresh
# every 7 days.  Zero cron, zero daemon, zero config.

set -euo pipefail

REPO="gaia-research/skill-cost"
RAW="https://raw.githubusercontent.com/${REPO}/main"
SKILL_NAME="cost"

if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD=''; DIM=''; GREEN=''; YELLOW=''; BLUE=''; RESET=''
fi

say()  { printf '%s\n' "$*"; }
info() { printf '%s->%s %s\n' "$BLUE"   "$RESET" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$GREEN"  "$RESET" "$*"; }
warn() { printf '%s[!]%s %s\n' "$YELLOW" "$RESET" "$*"; }

# ---------------------------------------------------------------------------
# Locate a skills directory (in preference order)
# ---------------------------------------------------------------------------
CANDIDATES=()
HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"
[ -d "$HOME/.pi/agent/skills" ]   && CANDIDATES+=("$HOME/.pi/agent/skills")
[ -d "$HOME/.claude/skills" ]     && CANDIDATES+=("$HOME/.claude/skills")
[ -d "$HOME/.codex/skills" ]      && CANDIDATES+=("$HOME/.codex/skills")
{ [ -d "$HERMES_ROOT/skills" ] || [ -f "$HERMES_ROOT/state.db" ]; } && CANDIDATES+=("$HERMES_ROOT/skills")
[ -d "$HOME/.agents/skills" ]     && CANDIDATES+=("$HOME/.agents/skills")
[ -d ".agents/skills" ]           && CANDIDATES+=(".agents/skills")
[ -d ".claude/skills" ]           && CANDIDATES+=(".claude/skills")

TARGET_DIR=""
if [ "${#CANDIDATES[@]}" -eq 0 ]; then
  info "No skills directory found. Creating ${BOLD}.agents/skills${RESET} in current dir."
  mkdir -p ".agents/skills"
  TARGET_DIR=".agents/skills"
elif [ "${#CANDIDATES[@]}" -eq 1 ]; then
  TARGET_DIR="${CANDIDATES[0]}"
  info "Detected skills directory: ${BOLD}${TARGET_DIR}${RESET}"
else
  say ""
  say "${BOLD}Multiple skills directories found. Where should ${SKILL_NAME} go?${RESET}"
  i=1
  for c in "${CANDIDATES[@]}"; do
    printf "  ${BOLD}%d)${RESET} %s\n" "$i" "$c"
    i=$((i + 1))
  done
  say ""
  printf "Select [1-${#CANDIDATES[@]}]: "
  read -r choice
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#CANDIDATES[@]}" ]; then
    warn "Invalid selection. Aborting."
    exit 1
  fi
  TARGET_DIR="${CANDIDATES[$((choice - 1))]}"
fi

INSTALL_DIR="${TARGET_DIR}/${SKILL_NAME}"

if [ -d "$INSTALL_DIR" ]; then
  warn "${BOLD}${INSTALL_DIR}${RESET} already exists."
  printf "Overwrite? [y/N]: "
  read -r reply
  case "$reply" in
    y|Y|yes|YES) rm -rf "$INSTALL_DIR" ;;
    *) info "Aborted. No changes made."; exit 0 ;;
  esac
fi

mkdir -p "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# Fetch files
# ---------------------------------------------------------------------------
for f in cost.py SKILL.md prices.json; do
  info "Fetching ${f}..."
  curl -fsSL "${RAW}/${f}" -o "${INSTALL_DIR}/${f}"
done
chmod +x "${INSTALL_DIR}/cost.py"

# ---------------------------------------------------------------------------
# Optional /cost prompt-template for pi
# ---------------------------------------------------------------------------
if [ -d "$HOME/.pi/agent/prompts" ]; then
  info "Installing pi /cost prompt template..."
  cat > "$HOME/.pi/agent/prompts/cost.md" <<'EOF'
---
description: Report token usage and public-rate USD cost from harness session stores (skill-cost)
argument-hint: "[--all|--today|--since DATE|--harness <id>|--session ID|--cwd PATH|--by-model|--list|--json|--refresh-prices]"
---
Load the `cost` skill and follow its instructions.

Run:

```bash
COST_PY="$(find ~/.pi/agent/skills ~/.claude/skills ~/.codex/skills "${HERMES_HOME:-$HOME/.hermes}/skills" ~/.agents/skills .agents/skills .claude/skills -type f -name 'cost.py' 2>/dev/null | head -1)"
if [ -z "$COST_PY" ]; then
  echo "cost.py not found in any configured skills directory" >&2
  exit 1
fi
python3 "$COST_PY" $ARGUMENTS
```

Then:
- Show the script's stdout verbatim in a fenced block.
- Summarize the grand-total cost and which harness/session it covers.
- If unpriced models are listed, suggest `--refresh-prices`.
- Do not invent numbers beyond what the script prints.
EOF
  ok "Installed /cost prompt template"
fi

ok "Installed to ${BOLD}${INSTALL_DIR}${RESET}"

# ---------------------------------------------------------------------------
# Post-install checks
# ---------------------------------------------------------------------------
say ""
say "${BOLD}Requirements check${RESET}"

if command -v python3 >/dev/null 2>&1; then
  PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  ok "python3 ${PYVER} available"
else
  warn "python3 not found -- cost requires Python 3.8+"
fi

# Sanity-check the install
if python3 "${INSTALL_DIR}/cost.py" --offline --all --list >/dev/null 2>&1; then
  ok "cost.py runs cleanly against local session stores"
else
  info "cost.py installed. No harness session stores were found yet -- that's fine."
fi

say ""
say "${BOLD}${GREEN}cost is ready.${RESET}  Prices auto-refresh every 7 days."
if [ "$TARGET_DIR" = "$HERMES_ROOT/skills" ]; then
  info "In an already-running Hermes session, run /reload-skills once."
fi
say ""
say "  ${DIM}# From an agent conversation:${RESET}"
say "  ${BOLD}/cost${RESET}                    ${DIM}# current session${RESET}"
say "  ${BOLD}/cost --all --list${RESET}       ${DIM}# every session, one line each${RESET}"
say ""
say "  ${DIM}# Directly:${RESET}"
say "  ${BOLD}python3 ${INSTALL_DIR}/cost.py${RESET}"
say ""
