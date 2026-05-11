#!/usr/bin/env bash
# Alpha Strategist — Linux Deployment Script (V1.1)
# Target: Debian/Ubuntu-based Linux workstation
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh          # full deploy (systemd if sudo available, tmux fallback)
#   ./deploy.sh --tmux   # force tmux mode (skip systemd even if sudo exists)
#   ./deploy.sh --status # show service status and recent logs
#
# What this script does:
#   1. Verifies / installs uv
#   2. Syncs dependencies  (uv sync)
#   3. Installs or restarts the systemd service (requires sudo)
#      OR launches in a named tmux session (no sudo needed)

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
die()     { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
SERVICE_NAME="warden"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_TEMPLATE="${PROJECT_DIR}/warden.service"
TMUX_SESSION="warden"
ENV_FILE="${PROJECT_DIR}/.env"

# ── Argument parsing ──────────────────────────────────────────────────────────
FORCE_TMUX=false
STATUS_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --tmux)   FORCE_TMUX=true  ;;
        --status) STATUS_ONLY=true ;;
        *) die "Unknown argument: $arg. Usage: $0 [--tmux|--status]" ;;
    esac
done

# ── Status mode ───────────────────────────────────────────────────────────────
if $STATUS_ONLY; then
    echo -e "\n${BOLD}=== Warden Service Status ===${RESET}"
    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        ok "systemd service '${SERVICE_NAME}' is RUNNING"
        systemctl status "${SERVICE_NAME}" --no-pager -l | tail -20
    elif tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
        ok "tmux session '${TMUX_SESSION}' is RUNNING"
        echo "  Attach with: tmux attach -t ${TMUX_SESSION}"
    else
        warn "Warden is NOT running (no systemd service or tmux session found)"
    fi
    exit 0
fi

echo -e "\n${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║  Alpha Strategist — Warden Deploy V1.1  ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}\n"

# ── Pre-flight: .env ──────────────────────────────────────────────────────────
if [[ ! -f "${ENV_FILE}" ]]; then
    warn ".env not found at ${ENV_FILE}"
    if [[ -f "${PROJECT_DIR}/.env.example" ]]; then
        echo "  → Copy and fill the template:"
        echo "      cp ${PROJECT_DIR}/.env.example ${ENV_FILE}"
        echo "      nano ${ENV_FILE}"
    fi
    die "Deployment aborted: .env is required."
fi
ok ".env found"

# ── Step 1: Check / install uv ────────────────────────────────────────────────
info "Checking for uv..."

UV_BIN=""
if command -v uv &>/dev/null; then
    UV_BIN="$(command -v uv)"
    ok "uv found at ${UV_BIN} ($(uv --version))"
elif [[ -x "${HOME}/.local/bin/uv" ]]; then
    UV_BIN="${HOME}/.local/bin/uv"
    ok "uv found at ${UV_BIN} ($(${UV_BIN} --version))"
    export PATH="${HOME}/.local/bin:${PATH}"
else
    warn "uv is not installed."
    echo ""
    echo "  Install uv now with:"
    echo -e "    ${BOLD}curl -LsSf https://astral.sh/uv/install.sh | sh${RESET}"
    echo "  Then re-run this script, or add ~/.local/bin to your PATH:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    read -rp "  Install uv automatically now? [y/N] " INSTALL_UV
    if [[ "${INSTALL_UV,,}" == "y" ]]; then
        info "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="${HOME}/.local/bin:${PATH}"
        UV_BIN="${HOME}/.local/bin/uv"
        ok "uv installed at ${UV_BIN}"
    else
        die "uv is required. Aborting."
    fi
fi

# ── Step 2: Sync latest code ──────────────────────────────────────────────────
info "Syncing code..."

cd "${PROJECT_DIR}"

if git -C "${PROJECT_DIR}" rev-parse --is-inside-work-tree &>/dev/null; then
    CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    info "git pull (branch: ${CURRENT_BRANCH})..."
    git pull --ff-only || warn "git pull failed — continuing with local code"
    ok "Code up to date ($(git rev-parse --short HEAD))"
else
    warn "Not a git repository — skipping git pull"
fi

info "Syncing Python dependencies with uv..."
"${UV_BIN}" sync --frozen
ok "Dependencies synced"

# ── Step 3: Deploy ────────────────────────────────────────────────────────────
HAS_SUDO=false
if ! $FORCE_TMUX && sudo -n true 2>/dev/null; then
    HAS_SUDO=true
fi

if $FORCE_TMUX; then
    info "Forced tmux mode (--tmux flag set)"
fi

# ── Path A: systemd (sudo available) ─────────────────────────────────────────
if $HAS_SUDO && ! $FORCE_TMUX; then
    info "sudo available — deploying as systemd service..."

    [[ -f "${SERVICE_TEMPLATE}" ]] || die "warden.service template not found at ${SERVICE_TEMPLATE}"

    # Substitute placeholders into a temp file, then install
    TMP_SERVICE="$(mktemp)"
    sed \
        -e "s|__USER__|${USER}|g" \
        -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
        -e "s|__UV_BIN__|${UV_BIN}|g" \
        "${SERVICE_TEMPLATE}" > "${TMP_SERVICE}"

    sudo cp "${TMP_SERVICE}" "${SERVICE_FILE}"
    rm -f "${TMP_SERVICE}"
    ok "Service file installed → ${SERVICE_FILE}"

    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}"

    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        info "Restarting running service..."
        sudo systemctl restart "${SERVICE_NAME}"
    else
        info "Starting service for the first time..."
        sudo systemctl start "${SERVICE_NAME}"
    fi

    sleep 2
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        ok "Warden service is RUNNING"
        echo ""
        echo -e "  ${BOLD}Useful commands:${RESET}"
        echo "    journalctl -u warden -f          # live logs"
        echo "    systemctl status warden          # service status"
        echo "    sudo systemctl stop warden       # stop"
        echo "    sudo systemctl disable warden    # disable autostart"
    else
        warn "Service started but is not active — check logs:"
        echo "    journalctl -u warden -xe --no-pager | tail -30"
        journalctl -u "${SERVICE_NAME}" -xe --no-pager 2>/dev/null | tail -20 || true
        exit 1
    fi

# ── Path B: tmux (no sudo) ────────────────────────────────────────────────────
else
    if ! $HAS_SUDO && ! $FORCE_TMUX; then
        warn "sudo not available — falling back to tmux session"
    fi

    if ! command -v tmux &>/dev/null; then
        die "tmux is not installed and sudo is unavailable.\n  Ask your sysadmin to install tmux:\n    sudo apt-get install -y tmux"
    fi

    if tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
        info "Killing existing tmux session '${TMUX_SESSION}'..."
        tmux kill-session -t "${TMUX_SESSION}"
    fi

    info "Starting Warden in tmux session '${TMUX_SESSION}'..."
    TMUX_CMD="${UV_BIN} run python main.py 2>&1 | tee warden.log; echo '[Warden exited] Press ENTER to close'; read"
    tmux new-session -d -s "${TMUX_SESSION}" -c "${PROJECT_DIR}" "${TMUX_CMD}"

    sleep 2
    if tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
        ok "Warden is RUNNING in tmux session '${TMUX_SESSION}'"
        echo ""
        echo -e "  ${BOLD}Useful commands:${RESET}"
        echo "    tmux attach -t ${TMUX_SESSION}   # attach to session"
        echo "    tmux kill-session -t ${TMUX_SESSION}  # stop"
        echo "    tail -f ${PROJECT_DIR}/warden.log     # live logs (file)"
        echo ""
        warn "Note: tmux sessions do NOT survive reboots."
        echo "  To auto-start on login, add to ~/.bashrc or ~/.profile:"
        echo "    [[ -z \"\$TMUX\" ]] && tmux has-session -t ${TMUX_SESSION} 2>/dev/null || \\"
        echo "      (cd ${PROJECT_DIR} && tmux new-session -d -s ${TMUX_SESSION} \\"
        echo "         \"${UV_BIN} run python main.py 2>&1 | tee warden.log\")"
    else
        die "tmux session failed to start. Check ${PROJECT_DIR}/warden.log"
    fi
fi

echo ""
ok "Deployment complete."
