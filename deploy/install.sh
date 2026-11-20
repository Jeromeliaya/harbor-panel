#!/bin/sh
set -eu

APP_DIR=${HARBOR_PANEL_DIR:-/opt/harbor-panel}
APP_USER=${HARBOR_PANEL_USER:-harbor}
WORKSPACE=${HARBOR_PANEL_WORKSPACE:-/srv/harbor-workspace}
STATE_DIR=${HARBOR_PANEL_STATE:-/var/lib/harbor-panel}

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root (for example: sudo sh deploy/install.sh)." >&2
    exit 1
fi

if [ ! -f /etc/os-release ]; then
    echo "Cannot detect Linux distribution: /etc/os-release is missing." >&2
    exit 1
fi
. /etc/os-release

case "${ID:-}:${ID_LIKE:-}" in
    debian:*|ubuntu:*|linuxmint:*|*:debian*)
        apt-get update
        apt-get install -y python3 python3-pip python3-venv
        ;;
    alpine:*)
        apk add --no-cache python3 py3-pip py3-virtualenv build-base linux-headers
        ;;
    rhel:*|centos:*|rocky:*|almalinux:*|fedora:*|*:rhel*|*:fedora*)
        if command -v dnf >/dev/null 2>&1; then
            dnf install -y python3 python3-pip
        elif command -v yum >/dev/null 2>&1; then
            yum install -y python3 python3-pip
        else
            echo "Neither dnf nor yum is available on this RPM-based system." >&2
            exit 1
        fi
        ;;
    *)
        echo "Unsupported distribution: ${PRETTY_NAME:-${ID:-unknown}}" >&2
        echo "Install Python 3.10+, pip, and a venv implementation manually, then rerun." >&2
        exit 1
        ;;
esac

PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "python3 was not installed or is not on PATH." >&2
    exit 1
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
    if command -v useradd >/dev/null 2>&1; then
        useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    else
        adduser -S -D -H "$APP_USER"
    fi
fi

if [ ! -f "$APP_DIR/server.py" ]; then
    echo "Harbor Panel source was not found at $APP_DIR." >&2
    echo "Set HARBOR_PANEL_DIR to the project directory before running this script." >&2
    exit 1
fi

install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$WORKSPACE" "$STATE_DIR"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    if ! "$PYTHON" -m venv "$APP_DIR/.venv" 2>/dev/null; then
        if ! command -v virtualenv >/dev/null 2>&1; then
            echo "Python venv support is unavailable; install virtualenv and rerun." >&2
            exit 1
        fi
        virtualenv -p "$PYTHON" "$APP_DIR/.venv"
    fi
fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/.venv" "$WORKSPACE" "$STATE_DIR"

echo "Harbor Panel dependencies and data directories are ready."
echo "The first start requires an administrator registration credential. Create /var/lib/harbor-panel/bootstrap.token with a random Base64 token and owner harbor:harbor, mode 0600."
if command -v systemctl >/dev/null 2>&1; then
    echo "systemd: cp $APP_DIR/deploy/harbor-panel.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now harbor-panel"
else
    echo "OpenRC: cp $APP_DIR/deploy/harbor-panel.openrc /etc/init.d/harbor-panel && rc-update add harbor-panel default && rc-service harbor-panel start"
fi
