#!/usr/bin/env bash
# Install Crystal Security Engine on Linux, macOS or WSL.
#
# Usage:
#   ./install.sh                      # dedicated .venv, tree-sitter included
#   ./install.sh --mode user          # pip install --user
#   ./install.sh --no-treesitter      # regex fallback parsers only
#   ./install.sh --with-solc          # also install solc via solc-select

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="venv"
EDITABLE="-e"
TREESITTER=1
WITH_SOLC=0
SOLC_VERSION="0.8.20"

cyan() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok() { printf '\033[32m    ok   %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    warn %s\033[0m\n' "$1"; }
fail() { printf '\033[31m    FAIL %s\033[0m\n' "$1"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --no-treesitter) TREESITTER=0; shift ;;
    --with-solc) WITH_SOLC=1; shift ;;
    --solc-version) SOLC_VERSION="$2"; shift 2 ;;
    --no-editable) EDITABLE=""; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) fail "unknown option: $1"; exit 2 ;;
  esac
done

echo
echo "Crystal Security Engine - installer"
echo "-----------------------------------"

# --- 1. Python -------------------------------------------------------------
cyan "Checking Python"
PYTHON=""
for candidate in python3 python; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  [[ -z "$version" ]] && continue
  major="${version%%.*}"; minor="${version##*.}"
  if [[ "$major" -gt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$minor" -ge 10 ]]; }; then
    PYTHON="$(command -v "$candidate")"
    ok "$PYTHON (Python $version)"
    break
  fi
  warn "$candidate is Python $version (need >= 3.10)"
done
if [[ -z "$PYTHON" ]]; then
  fail "No Python >= 3.10 found. Install it and re-run."
  exit 1
fi

# --- 2. Target environment -------------------------------------------------
PIP_TARGET=()
case "$MODE" in
  venv)
    VENV="$ROOT/.venv"
    if [[ ! -x "$VENV/bin/python" ]]; then
      cyan "Creating virtual environment at $VENV"
      "$PYTHON" -m venv "$VENV"
    fi
    PYTHON="$VENV/bin/python"
    ok "using $PYTHON"
    ;;
  user) PIP_TARGET=(--user) ;;
  system) ;;
  *) fail "unknown mode: $MODE (venv|user|system)"; exit 2 ;;
esac

cyan "Upgrading pip"
"$PYTHON" -m pip install --upgrade pip --quiet "${PIP_TARGET[@]}" || warn "pip upgrade failed; continuing"

# --- 3. Crystal ------------------------------------------------------------
SPEC="."
[[ "$TREESITTER" -eq 1 ]] && SPEC=".[treesitter]"
cyan "Installing crystal ($SPEC)"
if ! (cd "$ROOT" && "$PYTHON" -m pip install ${EDITABLE:+$EDITABLE} "$SPEC" "${PIP_TARGET[@]}"); then
  if [[ "$TREESITTER" -eq 1 ]]; then
    warn "tree-sitter extra failed; retrying core-only install"
    (cd "$ROOT" && "$PYTHON" -m pip install ${EDITABLE:+$EDITABLE} "." "${PIP_TARGET[@]}")
    warn "installed without tree-sitter (regex fallback parsers)"
  else
    fail "install failed"; exit 1
  fi
else
  ok "crystal installed"
fi

# --- 4. PATH ---------------------------------------------------------------
cyan "Checking that 'crystal' is callable"
SCRIPTS="$("$PYTHON" -c "import sysconfig; print(sysconfig.get_path('scripts'))")"
if [[ -x "$SCRIPTS/crystal" ]]; then
  ok "entry point at $SCRIPTS/crystal"
  if command -v crystal >/dev/null 2>&1; then
    ok "'crystal' resolves on PATH"
  else
    warn "'crystal' is not on PATH yet. Add:"
    echo "         export PATH=\"$SCRIPTS:\$PATH\""
    [[ "$MODE" == "venv" ]] && echo "         Or activate the venv: source $ROOT/.venv/bin/activate"
  fi
else
  warn "entry point not found; use '$PYTHON -m crystal.cli' instead"
fi

# --- 5. Optional tooling ---------------------------------------------------
if [[ "$WITH_SOLC" -eq 1 ]]; then
  cyan "Installing solc $SOLC_VERSION via solc-select"
  "$PYTHON" -m pip install solc-select --quiet "${PIP_TARGET[@]}"
  "$PYTHON" -m solc_select.__main__ install "$SOLC_VERSION" || warn "solc install failed"
  "$PYTHON" -m solc_select.__main__ use "$SOLC_VERSION" || warn "solc select failed"
fi

for tool in solc forge anvil medusa echidna halmos cargo; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool found"
  else warn "$tool not found (optional; Crystal degrades instead of failing)"; fi
done

# --- 6. Verify -------------------------------------------------------------
cyan "Running 'crystal doctor'"
set +e
"$PYTHON" -m crystal.cli doctor
DOCTOR=$?
set -e

echo
if [[ "$DOCTOR" -eq 0 ]]; then
  printf '\033[32mCrystal is ready.\033[0m\n'
  echo "  crystal scan ./examples --no-solc"
  echo "  crystal scan ./target --format sarif -o crystal.sarif"
else
  printf '\033[33mCrystal installed, but doctor reported a blocking issue above.\033[0m\n'
fi
exit "$DOCTOR"
