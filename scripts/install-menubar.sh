#!/usr/bin/env bash
#
# Install claude-swap's macOS menu bar app from this fork, and start it at login.
#
#   curl -fsSL https://raw.githubusercontent.com/Palmetto-Interactive-LLC/claude-swap/main/scripts/install-menubar.sh | bash
#
# Options go after: | bash -s -- --help

set -euo pipefail

REPO_URL="https://github.com/Palmetto-Interactive-LLC/claude-swap"
PYTHON_VERSION="3.13"
APP_BUNDLE="${CSWAP_APP_BUNDLE:-/Applications/claude-swap Menubar.app}"
SERVICE_LOG="$HOME/Library/Logs/com.cswap.menubar.err"

ref="main"
local_install=false
start_service=true
uninstall=false

say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
Install claude-swap's macOS menu bar app from this fork, and start it at login.
Run it again at any time to update. Your accounts are never touched.

Options:
  --ref REF      Install this branch, tag or commit (default: main)
  --local        Install from the checkout this script lives in
  --no-service   Install only. Don't start it now or at login
  --uninstall    Remove the app and service, and keep your accounts
  -h, --help     Show this help
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ref)
            [ $# -ge 2 ] || die "--ref needs a branch, tag or commit"
            ref="$2"
            shift 2
            ;;
        --local)
            local_install=true
            shift
            ;;
        --no-service)
            start_service=false
            shift
            ;;
        --uninstall)
            uninstall=true
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1 (see --help)"
            ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || die "the menu bar app is macOS only"

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return
    fi
    if command -v brew >/dev/null 2>&1; then
        say "Installing uv with Homebrew"
        brew install uv
        return
    fi
    die "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and run this again"
}

run_uninstall() {
    ensure_uv
    local bin
    bin="$(uv tool dir --bin)/cswap"
    if [ -x "$bin" ]; then
        say "Stopping the menu bar service"
        "$bin" menubar --uninstall-service || warn "couldn't remove the service"
    fi
    if uv tool list 2>/dev/null | grep -q '^claude-swap '; then
        say "Removing claude-swap"
        uv tool uninstall claude-swap
    fi
    if [ -d "$APP_BUNDLE" ]; then
        say "Removing $APP_BUNDLE"
        rm -rf "$APP_BUNDLE"
    fi
    say "Done. Your accounts are still in ~/.claude-swap-backup"
}

if [ "$uninstall" = true ]; then
    run_uninstall
    exit 0
fi

ensure_uv

if [ "$local_install" = true ]; then
    # Piped through curl there's no script file, so no checkout to install from.
    script_path="${BASH_SOURCE[0]:-}"
    [ -f "$script_path" ] || die "--local has to run from a checkout: scripts/install-menubar.sh --local"
    source_dir="$(cd "$(dirname "$script_path")/.." && pwd)"
    [ -f "$source_dir/pyproject.toml" ] || die "--local needs a claude-swap checkout, and $source_dir isn't one"
    spec="claude-swap[menubar] @ file://$source_dir"
    say "Installing from $source_dir"
else
    spec="claude-swap[menubar] @ git+$REPO_URL@$ref"
    say "Installing from $REPO_URL ($ref)"
fi

# uv's managed Python on purpose: on macOS 26 and later a Homebrew or python.org
# framework build runs but never draws the menu bar icon, and logs nothing.
uv tool install --force --reinstall --managed-python --python "$PYTHON_VERSION" "$spec"

bin_dir="$(uv tool dir --bin)"
cswap="$bin_dir/cswap"
[ -x "$cswap" ] || die "the install finished but $cswap is missing"

tool_python="$(uv tool dir)/claude-swap/bin/python"
"$tool_python" -c 'import AppKit' 2>/dev/null ||
    die "the install can't load AppKit, so the menu bar can't start"
if [ -n "$("$tool_python" -c 'import sys; print(getattr(sys, "_framework", ""))')" ]; then
    warn "this Python is a framework build, which may not draw the menu bar icon on macOS 26 or later"
fi

say "Installed $("$cswap" --version)"

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) warn "$bin_dir isn't on your PATH. Run 'uv tool update-shell' and open a new terminal" ;;
esac
found="$(command -v cswap || true)"
if [ -n "$found" ] && [ "$found" != "$cswap" ]; then
    warn "'cswap' in this shell runs $found, not $cswap. Remove the older install"
fi
command -v claude >/dev/null 2>&1 ||
    warn "Claude Code isn't on your PATH. The menu bar needs it to sign in to accounts"

if [ "$start_service" = true ]; then
    say "Starting the menu bar app, now and at every login"
    "$cswap" menubar --install-service

    # Give launchd a moment: a crash on launch would otherwise still read as running.
    sleep 3
    status="$("$cswap" menubar --service-status 2>&1 || true)"
    case "$status" in
        *"running (pid"*)
            say "${status%%$'\n'*}"
            ;;
        *)
            printf '%s\n' "$status" >&2
            [ -s "$SERVICE_LOG" ] && tail -5 "$SERVICE_LOG" >&2
            die "the menu bar service didn't start. Its log is at $SERVICE_LOG"
            ;;
    esac
fi

if [ "$start_service" = true ]; then
    printf '\nAll set. Look for the icon in your menu bar: a solid dot in front of a ring.\n'
else
    printf '\nInstalled. Run "cswap menubar" to start the app.\n'
fi

cat <<EOF

Next:
  1. Add the account you're logged in to now:  cswap add
  2. Log in to your next account in Claude Code, then run cswap add again.
     Don't /logout first, because that can revoke the saved login.
  3. Click the icon, then click an account to switch.

To update, run this script again.
EOF
