#!/bin/sh
set -eu

APP_DIR=${HARBOR_PANEL_DIR:-/opt/harbor-panel}
STATE_DIR=${HARBOR_PANEL_STATE:-/var/lib/harbor-panel}

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root (for example: sudo sh deploy/install.sh)." >&2
    exit 1
fi

case "$APP_DIR" in
    /*) ;;
    *) echo "HARBOR_PANEL_DIR must be an absolute path." >&2; exit 1 ;;
esac
case "$STATE_DIR" in
    /*) ;;
    *) echo "HARBOR_PANEL_STATE must be an absolute path." >&2; exit 1 ;;
esac

prepare_config_file() {
    config_file=$1
    if [ -L "$config_file" ]; then
        echo "Refusing a symbolic-link configuration file: $config_file" >&2
        return 1
    fi
    if [ -e "$config_file" ]; then
        if [ ! -f "$config_file" ] || [ "$(stat -c '%h' "$config_file")" != 1 ]; then
            echo "Refusing a non-regular or hard-linked configuration file: $config_file" >&2
            return 1
        fi
        chown root:root "$config_file" || return 1
        chmod 600 "$config_file" || return 1
    fi
}
prepare_config_file /etc/harbor-panel.paths || exit 1
case "$APP_DIR" in
    *//*|*/) echo "Installation paths must not contain repeated or trailing slashes." >&2; exit 1 ;;
esac
case "$STATE_DIR" in
    *//*|*/) echo "Installation paths must not contain repeated or trailing slashes." >&2; exit 1 ;;
esac
case "$APP_DIR$STATE_DIR" in
    *[!A-Za-z0-9_./-]*)
        echo "Installation paths must use safe absolute paths without traversal or whitespace." >&2
        exit 1
        ;;
esac
case "$APP_DIR" in
    */../*|*/..|*/./*|*/.)
        echo "Installation paths must not contain dot-segment traversal." >&2
        exit 1
        ;;
esac
case "$STATE_DIR" in
    */../*|*/..|*/./*|*/.)
        echo "Installation paths must not contain dot-segment traversal." >&2
        exit 1
        ;;
esac

has_symlink_component() {
    remaining=${1#/}
    current=
    while [ -n "$remaining" ]; do
        component=${remaining%%/*}
        current="$current/$component"
        [ -L "$current" ] && return 0
        [ "$remaining" = "$component" ] && break
        remaining=${remaining#*/}
    done
    return 1
}
if has_symlink_component "$APP_DIR" || has_symlink_component "$STATE_DIR"; then
    echo "Installation path components must not be symbolic links." >&2
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
        apt-get install -y python3 python3-pip python3-venv openssh-server
        ;;
    alpine:*)
        apk add --no-cache python3 py3-pip py3-virtualenv build-base linux-headers openssh-server
        ;;
    rhel:*|centos:*|rocky:*|almalinux:*|fedora:*|*:rhel*|*:fedora*)
        if command -v dnf >/dev/null 2>&1; then
            dnf install -y python3 python3-pip openssh-server
        elif command -v yum >/dev/null 2>&1; then
            yum install -y python3 python3-pip openssh-server
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
APP_DIR=$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$APP_DIR")
STATE_DIR=$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$STATE_DIR")
case "$APP_DIR" in
    /|/etc|/home|/home/*|/opt|/root|/run|/srv|/sys|/tmp|/tmp/*|/var/tmp|/var/tmp/*|/usr|/var|/dev|/proc)
        echo "HARBOR_PANEL_DIR must resolve to a dedicated application directory." >&2
        exit 1
        ;;
esac
case "$STATE_DIR" in
    /|/etc|/home|/home/*|/opt|/root|/run|/srv|/sys|/tmp|/tmp/*|/var/tmp|/var/tmp/*|/usr|/var|/var/lib|/var/log|/var/cache|/dev|/proc)
        echo "HARBOR_PANEL_STATE must resolve to a dedicated non-system directory." >&2
        exit 1
        ;;
esac
if ! "$PYTHON" -c 'import os,sys; app,state=sys.argv[1:]; common=os.path.commonpath((app,state)); sys.exit(common in (app,state))' "$APP_DIR" "$STATE_DIR"; then
    echo "HARBOR_PANEL_DIR and HARBOR_PANEL_STATE must be separate, non-overlapping directories." >&2
    exit 1
fi

if [ ! -f "$APP_DIR/server.py" ]; then
    echo "Harbor Panel source was not found at $APP_DIR." >&2
    echo "Set HARBOR_PANEL_DIR to the project directory before running this script." >&2
    exit 1
fi

if [ -L "$APP_DIR/.venv" ]; then
    echo "Refusing a symbolic-link virtual environment: $APP_DIR/.venv" >&2
    exit 1
fi

if find "$APP_DIR" -xdev -type f -links +1 -print -quit | grep -q .; then
    echo "Refusing hard-linked files inside the application directory." >&2
    exit 1
fi

if ! "$PYTHON" -c 'import os, pathlib, stat, sys; root=pathlib.Path(sys.argv[1]); bad=[]
for current, dirs, files in os.walk(root, followlinks=False):
 for name in dirs+files:
  item=pathlib.Path(current)/name
  if not item.is_symlink(): continue
  try: target=item.resolve(strict=True)
  except (OSError, RuntimeError): bad.append(str(item)); continue
  ancestor=target
  while True:
   info=ancestor.stat()
   if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH): bad.append(str(item)); break
   if ancestor == ancestor.parent: break
   ancestor=ancestor.parent
sys.exit(1 if bad else 0)' "$APP_DIR"; then
    echo "Application symlinks must resolve only to root-owned, non-writable files and directories." >&2
    exit 1
fi

if ! "$PYTHON" -c 'import os, pathlib, stat, sys; paths=sys.argv[1:]; bad=[]
for path in paths:
 current=pathlib.Path(path).parent
 while current != current.parent:
  try: info=current.stat()
  except FileNotFoundError: current=current.parent; continue
  if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH): bad.append(str(current))
  current=current.parent
sys.exit(1 if bad else 0)' "$APP_DIR" "$STATE_DIR"; then
    echo "Parent directories of the application and state paths must be root-owned and not writable by group/other." >&2
    exit 1
fi

find "$APP_DIR" -xdev \( -type d -o -type f \) -exec chown root:root {} +
find "$APP_DIR" -xdev \( -type d -o -type f \) -exec chmod go-w {} +

install -d -m 700 "$STATE_DIR"
chown root:root "$STATE_DIR"
chmod 700 "$STATE_DIR"
for name in auth.sqlite3 auth.sqlite3-wal auth.sqlite3-shm audit.sqlite3 bootstrap.token; do
    file="$STATE_DIR/$name"
    if [ -e "$file" ] || [ -L "$file" ]; then
        if [ -L "$file" ] || [ ! -f "$file" ]; then
            echo "Refusing non-regular state file: $file" >&2
            exit 1
        fi
        chown root:root "$file"
        chmod 600 "$file"
    fi
done
umask 077
printf 'HARBOR_PANEL_DIR=%s\nHARBOR_PANEL_STATE=%s\n' "$APP_DIR" "$STATE_DIR" > /etc/harbor-panel.paths
chmod 600 /etc/harbor-panel.paths
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
echo "Harbor Panel dependencies and data directories are ready."
echo "Native Linux services run as root and can administer the host. Protect the panel with a firewall or HTTPS reverse proxy."
echo "The first start requires a one-time registration credential. The management menu creates it automatically; manual installs can use the server's bootstrap-token preparation helper."
if command -v systemctl >/dev/null 2>&1; then
    echo "systemd: cp $APP_DIR/deploy/harbor-panel.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now harbor-panel"
else
    echo "OpenRC: cp $APP_DIR/deploy/harbor-panel.openrc /etc/init.d/harbor-panel && rc-update add harbor-panel default && rc-service harbor-panel start"
fi
