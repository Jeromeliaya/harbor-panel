#!/bin/sh
set -eu

REPOSITORY=${HARBOR_PANEL_REPOSITORY:-https://github.com/Jeromeliaya/harbor-panel}
BRANCH=${HARBOR_PANEL_BRANCH:-main}
APP_DIR=${HARBOR_PANEL_DIR:-/opt/harbor-panel}
ARCHIVE_URL="$REPOSITORY/archive/refs/heads/$BRANCH.tar.gz"
TMP_DIR=
PORT_FILE=/etc/harbor-panel.port
TOKEN_FILE=/var/lib/harbor-panel/bootstrap.token

cleanup() {
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT INT TERM

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 运行。直接下载运行时可执行: curl -fsSL URL -o /tmp/harbor-panel-menu.sh && sudo sh /tmp/harbor-panel-menu.sh" >&2
    exit 1
fi

download() {
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 10 "$ARCHIVE_URL" -o "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$1" "$ARCHIVE_URL"
    else
        echo "未找到 curl 或 wget。请先安装其中一个。" >&2
        exit 1
    fi
}

fetch_source() {
    TMP_DIR=$(mktemp -d)
    archive="$TMP_DIR/harbor-panel.tar.gz"
    download "$archive"
    tar -xzf "$archive" -C "$TMP_DIR"
    source_dir=$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'harbor-panel-*' -print -quit)
    if [ -z "$source_dir" ] || [ ! -f "$source_dir/server.py" ]; then
        echo "下载的 Harbor Panel 源码无效。" >&2
        exit 1
    fi
    install -d "$APP_DIR"
    cp -R "$source_dir"/. "$APP_DIR"/
}

read_port() {
    while :; do
        printf '请输入服务端口 (0-65535，默认 8088): ' >&2
        read port || port=
        port=${port:-8088}
        case "$port" in
            *[!0-9]*) echo "端口必须是 0 到 65535 的整数。" >&2 ;;
            *)
                if [ "${#port}" -gt 5 ] || [ "$port" -gt 65535 ]; then
                    echo "端口必须在 0 到 65535 之间。" >&2
                else
                    printf '%s\n' "$port"
                    return 0
                fi
                ;;
        esac
    done
}

configure_port() {
    selected_port=$1
    umask 077
    printf 'HARBOR_PANEL_PORT=%s\n' "$selected_port" > "$PORT_FILE"
    if command -v systemctl >/dev/null 2>&1; then
        install -m 644 "$APP_DIR/deploy/harbor-panel.service" /etc/systemd/system/harbor-panel.service
    elif [ -f /etc/init.d/harbor-panel ] || [ -f /sbin/rc-service ]; then
        install -m 755 "$APP_DIR/deploy/harbor-panel.openrc" /etc/init.d/harbor-panel
    fi
}

install_panel() {
    selected_port=$(read_port)
    if [ -f /var/lib/harbor-panel/auth.sqlite3 ]; then
        echo "检测到已有管理员账户，安装不会重新生成注册凭证。"
        bootstrap_token=
    else
        bootstrap_token=
    fi
    fetch_source
    HARBOR_PANEL_DIR="$APP_DIR" sh "$APP_DIR/deploy/install.sh"
    if [ ! -f /var/lib/harbor-panel/auth.sqlite3 ]; then
        bootstrap_token=$(dd if=/dev/urandom bs=32 count=1 2>/dev/null | base64 | tr -d '\n')
        umask 077
        printf '%s\n' "$bootstrap_token" > "$TOKEN_FILE"
        chown harbor:harbor "$TOKEN_FILE"
        chmod 600 "$TOKEN_FILE"
    fi
    configure_port "$selected_port"
    if command -v systemctl >/dev/null 2>&1; then
        install -m 644 "$APP_DIR/deploy/harbor-panel.service" /etc/systemd/system/harbor-panel.service
        systemctl daemon-reload
        systemctl enable --now harbor-panel
    elif [ -x "$APP_DIR/deploy/harbor-panel.openrc" ] || [ -f /sbin/rc-service ]; then
        install -m 755 "$APP_DIR/deploy/harbor-panel.openrc" /etc/init.d/harbor-panel
        rc-update add harbor-panel default
        rc-service harbor-panel restart 2>/dev/null || rc-service harbor-panel start
    else
        echo "未检测到 systemd 或 OpenRC，源码已安装，请手动启动服务。"
    fi
    echo "安装完成，访问 http://127.0.0.1:$selected_port。"
    if [ -n "${bootstrap_token:-}" ]; then
        echo "首次注册一次性凭证（Base64），请立即安全保存："
        printf '%s\n' "$bootstrap_token"
    fi
}

update_panel() {
    if [ ! -f "$APP_DIR/server.py" ]; then
        echo "尚未安装 Harbor Panel，请先选择 1。"
        return
    fi
    fetch_source
    HARBOR_PANEL_DIR="$APP_DIR" sh "$APP_DIR/deploy/install.sh"
    selected_port=8088
    if [ -f "$PORT_FILE" ]; then
        . "$PORT_FILE"
        selected_port=${HARBOR_PANEL_PORT:-8088}
    fi
    configure_port "$selected_port"
    if command -v systemctl >/dev/null 2>&1; then
        install -m 644 "$APP_DIR/deploy/harbor-panel.service" /etc/systemd/system/harbor-panel.service
        systemctl daemon-reload
        systemctl restart harbor-panel
    elif [ -f /etc/init.d/harbor-panel ]; then
        install -m 755 "$APP_DIR/deploy/harbor-panel.openrc" /etc/init.d/harbor-panel
        rc-service harbor-panel restart
    fi
    echo "更新完成，配置文件、工作区和审计数据已保留。"
}

uninstall_panel() {
    if [ ! -f "$APP_DIR/server.py" ]; then
        echo "未发现已安装的 Harbor Panel。"
        return
    fi
    case "$APP_DIR" in
        /|/opt|/srv|/usr|/etc|/var|/home)
            echo "拒绝卸载不安全的程序目录: $APP_DIR" >&2
            return 1
            ;;
    esac
    printf '确认卸载程序文件（保留工作区、审计数据和密码配置）请输入 UNINSTALL: '
    read answer
    if [ "$answer" != "UNINSTALL" ]; then
        echo "已取消。"
        return
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now harbor-panel 2>/dev/null || true
        rm -f /etc/systemd/system/harbor-panel.service
        systemctl daemon-reload
    elif [ -f /etc/init.d/harbor-panel ]; then
        rc-service harbor-panel stop 2>/dev/null || true
        rc-update del harbor-panel default 2>/dev/null || true
        rm -f /etc/init.d/harbor-panel
    fi
    rm -rf "$APP_DIR"
    id harbor >/dev/null 2>&1 && userdel harbor 2>/dev/null || true
    echo "程序已卸载。数据仍保留在 /srv/harbor-workspace、/var/lib/harbor-panel 和 /etc/harbor-panel.env。"
}

while :; do
    printf '\nHarbor Panel 管理菜单\n'
    printf '1. 安装\n2. 更新\n3. 卸载\n0. 退出\n\n请选择: '
    read choice
    case "$choice" in
        1) install_panel ;;
        2) update_panel ;;
        3) uninstall_panel ;;
        0) exit 0 ;;
        *) echo "无效选项，请输入 1、2、3 或 0。" ;;
    esac
done
