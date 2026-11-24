#!/bin/sh
set -eu

REPOSITORY=${HARBOR_PANEL_REPOSITORY:-https://github.com/Jeromeliaya/harbor-panel}
BRANCH=${HARBOR_PANEL_BRANCH:-main}
APP_DIR=${HARBOR_PANEL_DIR:-}
STATE_DIR=${HARBOR_PANEL_STATE:-}
ARCHIVE_URL="$REPOSITORY/archive/refs/heads/$BRANCH.tar.gz"
TMP_DIR=
PORT_FILE=/etc/harbor-panel.port
SERVICE_WAS_RUNNING=0
CALLER_PATH=${PATH:-}
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

if [ -L /etc/harbor-panel.paths ] || [ -L "$PORT_FILE" ]; then
    echo "Service path and port configuration files must not be symbolic links." >&2
    exit 1
fi
if [ -r /etc/harbor-panel.paths ]; then
    while IFS='=' read -r key value; do
        case "$key" in
            HARBOR_PANEL_DIR) [ -n "$APP_DIR" ] || APP_DIR=$value ;;
            HARBOR_PANEL_STATE) [ -n "$STATE_DIR" ] || STATE_DIR=$value ;;
        esac
    done < /etc/harbor-panel.paths
fi
APP_DIR=${APP_DIR:-/opt/harbor-panel}
STATE_DIR=${STATE_DIR:-/var/lib/harbor-panel}

case "$APP_DIR" in
    /*) ;;
    *) echo "HARBOR_PANEL_DIR must be an absolute path." >&2; exit 1 ;;
esac
case "$STATE_DIR" in
    /*) ;;
    *) echo "HARBOR_PANEL_STATE must be an absolute path." >&2; exit 1 ;;
esac
case "$APP_DIR$STATE_DIR" in
    *[!A-Za-z0-9_./-]*)
        echo "Installation paths must use safe absolute paths without traversal or whitespace." >&2
        exit 1
        ;;
esac
case "$APP_DIR" in
    *//*|*/) echo "Installation paths must not contain repeated or trailing slashes." >&2; exit 1 ;;
esac
case "$STATE_DIR" in
    *//*|*/) echo "Installation paths must not contain repeated or trailing slashes." >&2; exit 1 ;;
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
case "$APP_DIR" in
    /|/etc|/home|/home/*|/opt|/root|/run|/srv|/sys|/tmp|/tmp/*|/var/tmp|/var/tmp/*|/usr|/var|/dev|/proc|*/../*|*/..)
        echo "HARBOR_PANEL_DIR must be a dedicated application directory." >&2
        exit 1
        ;;
esac
case "$STATE_DIR" in
    /|/etc|/home|/home/*|/opt|/root|/run|/srv|/sys|/tmp|/tmp/*|/var/tmp|/var/tmp/*|/var|/var/lib|/var/log|/var/cache|/dev|/proc)
        echo "HARBOR_PANEL_STATE must be a dedicated state directory." >&2
        exit 1
        ;;
esac
for directory in "$APP_DIR" "$STATE_DIR"; do
    case "$directory" in
        /etc/*|/usr/*|/bin/*|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/proc/*|/sys/*|/dev/*|/run/*|/boot|/boot/*|/var/lib|/var/log|/var/cache|/var/lib/docker|/var/lib/docker/*|/var/lib/containerd|/var/lib/containerd/*|/root/harbor-panel-backups|/root/harbor-panel-backups/*)
            echo "安装路径必须是面板专用目录: $directory" >&2
            exit 1
            ;;
    esac
done
case "$APP_DIR/" in
    "$STATE_DIR/"*) echo "Application and state paths must not overlap." >&2; exit 1 ;;
esac
case "$STATE_DIR/" in
    "$APP_DIR/"*) echo "Application and state paths must not overlap." >&2; exit 1 ;;
esac

cleanup() {
    if [ "$SERVICE_WAS_RUNNING" -eq 1 ]; then
        restart_previous_service || true
    fi
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT INT TERM

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

safe_root_parent() {
    current=${1%/*}
    [ -n "$current" ] || current=/
    while [ ! -e "$current" ]; do
        current=${current%/*}
        [ -n "$current" ] || current=/
    done
    while :; do
        [ -d "$current" ] || return 1
        metadata=$(stat -c '%u %a' "$current") || return 1
        set -- $metadata
        owner=$1
        mode=$2
        permissions=${mode#${mode%???}}
        group=${permissions#?}
        group=${group%?}
        other=${permissions#??}
        [ "$owner" = 0 ] || return 1
        case "$group:$other" in
            *[2367]*:*|*:*[2367]*) return 1 ;;
        esac
        [ "$current" = / ] && break
        current=${current%/*}
        [ -n "$current" ] || current=/
    done
}

if [ "$(id -u)" -ne 0 ]; then
    echo '请先以 root 登录，或运行 sudo -i，再到仅 root 可写的目录按 README 下载并执行 ./harbor-panel-menu.sh。' >&2
    exit 1
fi
if has_symlink_component "$APP_DIR" || has_symlink_component "$STATE_DIR"; then
    echo "Installation path components must not be symbolic links." >&2
    exit 1
fi
if ! safe_root_parent "$APP_DIR" || ! safe_root_parent "$STATE_DIR"; then
    echo "Parent directories of the application and state paths must be root-owned and not writable by group/other." >&2
    exit 1
fi

helper_safe() {
    set -- $1
    [ "$1" = 0 ] && [ "$3" = 1 ] || return 1
    permissions=${2#${2%???}}
    group=${permissions#?}
    group=${group%?}
    other=${permissions#??}
    case "$group:$other" in
        *[2367]*:*|*:*[2367]*) return 1 ;;
    esac
}

menu_admin() {
    helper="$APP_DIR/deploy/menu_admin.py"
    if [ ! -f "$helper" ]; then
        echo "此功能需要新版菜单组件。请先选择 2 更新，再运行安装后的菜单。" >&2
        return 1
    fi
    if has_symlink_component "$helper" || ! safe_root_parent "$helper"; then
        echo "拒绝运行未受保护的菜单组件。" >&2
        return 1
    fi
    metadata=$(stat -c '%u %a %h' "$helper") || return 1
    operation=$1
    shift
    helper_safe "$metadata" || return 1
    python3 -I "$helper" "$operation" "$APP_DIR" "$STATE_DIR" "$@"
}

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
    TMP_DIR=$(mktemp -d) || return 1
    archive="$TMP_DIR/harbor-panel.tar.gz"
    download "$archive" || return 1
    tar -xzf "$archive" -C "$TMP_DIR" || return 1
    source_dir=$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'harbor-panel-*' -print -quit) || return 1
    if [ -z "$source_dir" ] || [ ! -f "$source_dir/server.py" ]; then
        echo "下载的 Harbor Panel 源码无效。" >&2
        return 1
    fi
    if find "$source_dir" -xdev \( -type l -o -type f -links +1 \) -print -quit | grep -q .; then
        echo "下载的源码包含不支持的符号链接或硬链接。" >&2
        return 1
    fi
    install -d "$APP_DIR" || return 1
    if [ -L "$APP_DIR" ] || [ ! -d "$APP_DIR" ]; then
        echo "应用路径必须是普通目录。" >&2
        return 1
    fi
    if [ -L "$APP_DIR/.venv" ] || { [ -e "$APP_DIR/.venv" ] && [ ! -d "$APP_DIR/.venv" ]; }; then
        echo "拒绝使用符号链接或非目录虚拟环境。" >&2
        return 1
    fi
    if find "$APP_DIR" -xdev -path "$APP_DIR/.venv" -prune -o -type l -print -quit | grep -q .; then
        echo "应用目录中存在符号链接；请先检查专用程序目录。" >&2
        return 1
    fi
    if find "$APP_DIR" -xdev -type f -links +1 -print -quit | grep -q .; then
        echo "应用目录中存在硬链接；请先检查专用程序目录。" >&2
        return 1
    fi
    chown root:root "$APP_DIR" || return 1
    chmod go-w "$APP_DIR" || return 1
    find "$APP_DIR" -xdev \( -type d -o -type f \) -exec chown root:root {} + || return 1
    find "$APP_DIR" -xdev \( -type d -o -type f \) -exec chmod go-w {} + || return 1
    cp -R "$source_dir"/. "$APP_DIR"/ || return 1
    rm -rf "$TMP_DIR" || return 1
    TMP_DIR=
}

prepare_config_file() {
    config_file=$1
    if [ -L "$config_file" ]; then
        echo "Refusing symbolic-link configuration file: $config_file" >&2
        return 1
    fi
    if [ -e "$config_file" ]; then
        if [ ! -f "$config_file" ] || [ "$(stat -c '%h' "$config_file")" != 1 ]; then
            echo "Refusing non-regular or hard-linked configuration file: $config_file" >&2
            return 1
        fi
        chown root:root "$config_file" || return 1
        chmod 600 "$config_file" || return 1
    fi
}

restart_previous_service() {
    [ "$SERVICE_WAS_RUNNING" -eq 1 ] || return 0
    if command -v systemctl >/dev/null 2>&1; then
        if ! systemctl start harbor-panel; then
            echo "Failed to restore the previously running Harbor Panel service." >&2
            return 1
        fi
    elif [ -f /etc/init.d/harbor-panel ]; then
        if ! rc-service harbor-panel start; then
            echo "Failed to restore the previously running Harbor Panel service." >&2
            return 1
        fi
    else
        echo "Cannot restore the previously running Harbor Panel service: service manager unavailable." >&2
        return 1
    fi
    SERVICE_WAS_RUNNING=0
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
    prepare_config_file "$PORT_FILE" || return 1
    prepare_config_file /etc/harbor-panel.paths || return 1
    umask 077
    printf 'HARBOR_PANEL_PORT=%s\n' "$selected_port" > "$PORT_FILE"
    printf 'HARBOR_PANEL_DIR=%s\nHARBOR_PANEL_STATE=%s\n' "$APP_DIR" "$STATE_DIR" > /etc/harbor-panel.paths
    chmod 600 "$PORT_FILE" /etc/harbor-panel.paths
    if command -v systemctl >/dev/null 2>&1; then
        install -m 644 "$APP_DIR/deploy/harbor-panel.service" /etc/systemd/system/harbor-panel.service
    elif [ -f /etc/init.d/harbor-panel ] || [ -f /sbin/rc-service ]; then
        install -m 755 "$APP_DIR/deploy/harbor-panel.openrc" /etc/init.d/harbor-panel
    fi
}

start_systemd_service() {
    action=$1
    if ! systemctl "$action" harbor-panel; then
        echo "Harbor Panel 服务启动或重启失败。systemd 状态和最近日志：" >&2
        systemctl status --no-pager --full harbor-panel >&2 || true
        journalctl --no-pager -n 40 -u harbor-panel >&2 || true
        return 1
    fi
}

print_access_url() {
    selected_port=$1
    public_ip=
    if command -v curl >/dev/null 2>&1; then
        public_ip=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)
    elif command -v wget >/dev/null 2>&1; then
        public_ip=$(wget -qO- -T 5 https://api.ipify.org 2>/dev/null || true)
    fi
    case "$public_ip" in
        *[!0-9.]*|'')
            echo "安装完成，服务端口：$selected_port。请使用 VPS 公网 IP 访问：http://<VPS公网IP>:$selected_port"
            ;;
        *)
            echo "安装完成，访问 http://$public_ip:$selected_port。"
            ;;
    esac
    echo "请确认 VPS 云安全组和系统防火墙已放行 TCP $selected_port。"
}

install_panel() {
    if [ -f "$APP_DIR/server.py" ]; then
        echo "检测到已安装的 Harbor Panel。请选择更新，或先卸载后再重新安装以重置注册凭证。"
        return
    fi
    if [ -e "$APP_DIR" ] && [ ! -d "$APP_DIR" ]; then
        echo "应用路径已存在且不是目录: $APP_DIR" >&2
        return 1
    fi
    if [ -d "$APP_DIR" ] && [ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        echo "应用目录非空但不是 Harbor Panel；请先检查目录内容，避免覆盖用户文件。" >&2
        return 1
    fi
    if [ -e "$STATE_DIR" ] && { [ ! -d "$STATE_DIR" ] || [ -n "$(find "$STATE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; }; then
        echo "状态目录非空或不是普通目录；请选择新的面板专用状态目录。" >&2
        return 1
    fi
    for directory in "$APP_DIR" "$STATE_DIR"; do
        if [ -d "$directory" ] && ! safe_root_parent "$directory/.harbor-panel-install-v1"; then
            echo "拒绝安装到未受保护的目录: $directory" >&2
            return 1
        fi
    done
    selected_port=$(read_port)
    fetch_source
    HARBOR_PANEL_DIR="$APP_DIR" HARBOR_PANEL_STATE="$STATE_DIR" sh "$APP_DIR/deploy/install.sh"
    bootstrap_token=$(PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python" -c 'from server import PanelServer; import sys; path=PanelServer.prepare_bootstrap_token(sys.argv[1]); print(path.read_text(encoding="ascii") if path else "")' "$STATE_DIR")
    configure_port "$selected_port"
    menu_admin claim "$0"
    if command -v systemctl >/dev/null 2>&1; then
        install -m 644 "$APP_DIR/deploy/harbor-panel.service" /etc/systemd/system/harbor-panel.service
        systemctl daemon-reload
        systemctl enable harbor-panel
        start_systemd_service start
    elif [ -x "$APP_DIR/deploy/harbor-panel.openrc" ] || [ -f /sbin/rc-service ]; then
        install -m 755 "$APP_DIR/deploy/harbor-panel.openrc" /etc/init.d/harbor-panel
        rc-update add harbor-panel default
        rc-service harbor-panel restart 2>/dev/null || rc-service harbor-panel start
    else
        echo "未检测到 systemd 或 OpenRC，源码已安装，请手动启动服务。"
    fi
    print_access_url "$selected_port"
    echo "警告：原生部署以 root 运行，可完全控制 VPS。公网 HTTP 不加密，可能泄露密码和登录会话；请限制防火墙来源，或自行配置 Nginx 与 HTTPS。"
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
    if [ "$(cat /etc/harbor-panel.paths 2>/dev/null)" != "$(printf 'HARBOR_PANEL_DIR=%s\nHARBOR_PANEL_STATE=%s\n' "$APP_DIR" "$STATE_DIR")" ]; then
        echo "更新必须使用已安装服务记录的路径。" >&2
        return 1
    fi
    if command -v systemctl >/dev/null 2>&1; then
        if [ -f /etc/systemd/system/harbor-panel.service ] && ! cmp -s /etc/systemd/system/harbor-panel.service "$APP_DIR/deploy/harbor-panel.service"; then
            echo "检测到手动修改的 Harbor Panel 服务文件，更新已停止，未覆盖配置。请先备份并迁移自定义设置。" >&2
            return 1
        fi
        if systemctl is-active --quiet harbor-panel; then
            systemctl stop harbor-panel
            SERVICE_WAS_RUNNING=1
        fi
    elif [ -f /etc/init.d/harbor-panel ]; then
        if ! cmp -s /etc/init.d/harbor-panel "$APP_DIR/deploy/harbor-panel.openrc"; then
            echo "检测到手动修改的 Harbor Panel 服务文件，更新已停止，未覆盖配置。请先备份并迁移自定义设置。" >&2
            return 1
        fi
        if rc-service harbor-panel status >/dev/null 2>&1; then
            rc-service harbor-panel stop
            SERVICE_WAS_RUNNING=1
        fi
    fi
    if ! fetch_source; then
        echo "更新源码失败。" >&2
        restart_previous_service || echo "The service may remain stopped; inspect the service manager logs." >&2
        return 1
    fi
    if ! HARBOR_PANEL_DIR="$APP_DIR" HARBOR_PANEL_STATE="$STATE_DIR" sh "$APP_DIR/deploy/install.sh"; then
        echo "依赖或权限迁移失败。" >&2
        restart_previous_service || echo "The service may remain stopped; inspect the service manager logs." >&2
        return 1
    fi
    selected_port=8088
    if [ -f "$PORT_FILE" ]; then
        while IFS='=' read -r key value; do
            if [ "$key" = HARBOR_PANEL_PORT ]; then
                case "$value" in
                    ''|*[!0-9]*) echo "Ignoring invalid stored service port." >&2 ;;
                    *) [ "${#value}" -le 5 ] && [ "$value" -le 65535 ] && selected_port=$value ;;
                esac
                break
            fi
        done < "$PORT_FILE"
    fi
    configure_port "$selected_port"
    menu_admin adopt "$0" || echo "未能验证卸载目录归属；请检查上述错误后再使用卸载。" >&2
    if command -v systemctl >/dev/null 2>&1; then
        install -m 644 "$APP_DIR/deploy/harbor-panel.service" /etc/systemd/system/harbor-panel.service
        systemctl daemon-reload
        start_systemd_service restart
        SERVICE_WAS_RUNNING=0
    elif [ -f /etc/init.d/harbor-panel ]; then
        install -m 755 "$APP_DIR/deploy/harbor-panel.openrc" /etc/init.d/harbor-panel
        rc-service harbor-panel restart
        SERVICE_WAS_RUNNING=0
    fi
    echo "警告：原生部署以 root 运行，可完全控制 VPS。公网 HTTP 不加密，可能泄露密码和登录会话；请限制防火墙来源，或自行配置 Nginx 与 HTTPS。"
    echo "更新完成，配置文件、工作区和审计数据已保留。"
}

set_shortcut() {
    printf '请输入自定义快捷命令（可以使用中文）: '
    IFS= read -r shortcut || return 0
    menu_admin shortcut "$shortcut" "$CALLER_PATH" "$0"
}

uninstall_panel() (
    # The installed helper may be missing or older than this downloaded menu.
    helper_sha='16d253aa952a549182df702463546e0e8d81a1c2ced229134704da834fc26878'
    helper="$APP_DIR/deploy/menu_admin.py"
    digest_matches() {
        actual=$(sha256sum < "$1") && [ "${actual%% *}" = "$helper_sha" ]
    }
    if ! command -v python3 >/dev/null 2>&1 || ! command -v sha256sum >/dev/null 2>&1; then
        echo '清理需要 python3 和 sha256sum；未删除任何文件。' >&2
        return 1
    fi
    if ! { [ -f "$helper" ] && ! has_symlink_component "$helper" && safe_root_parent "$helper" &&
           metadata=$(stat -c '%u %a %h' "$helper") && helper_safe "$metadata" && digest_matches "$helper"; }; then
        cleanup_dir=$(mktemp -d /tmp/harbor-panel-cleanup.XXXXXXXX) || return 1
        trap 'rm -rf -- "$cleanup_dir"' EXIT
        trap 'exit 1' HUP INT TERM
        helper="$cleanup_dir/menu_admin.py"
        url='https://raw.githubusercontent.com/Jeromeliaya/harbor-panel/main/deploy/menu_admin.py'
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL --connect-timeout 10 --max-time 60 "$url" -o "$helper" || return 1
        elif command -v wget >/dev/null 2>&1; then
            wget -q -T 30 -t 2 -O "$helper" "$url" || return 1
        else
            echo '缺少可用的清理组件；请安装 curl 或 wget 后重试。' >&2
            return 1
        fi
        if ! digest_matches "$helper"; then
            echo '清理组件版本不匹配；请重新下载最新版菜单后重试。未删除任何文件。' >&2
            return 1
        fi
    fi
    python3 -I "$helper" cleanup "$APP_DIR" "$STATE_DIR" "$0"
)

while :; do
    printf '\nHarbor Panel 管理菜单\n'
    printf '1. 安装\n2. 更新\n3. 卸载\n5. 设置面板快捷\n0. 退出\n\n请选择: '
    IFS= read -r choice || exit 0
    case "$choice" in
        1) install_panel ;;
        2) update_panel ;;
        3) if uninstall_panel; then exit 0; fi ;;
        5) set_shortcut || : ;;
        0) exit 0 ;;
        *) echo "无效选项，请输入 1、2、3、5 或 0。" ;;
    esac
done
