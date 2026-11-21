# Harbor Panel

从零编写的轻量单机运维面板。采用 **Python + 原生 JavaScript/CSS**，无需 Node 构建工具，只有一个 Python 依赖 `psutil`。可在 Windows 本地运行，也可原生部署在 Linux 上。

本项目使用原创代码、界面与品牌，提供轻量的单机运维管理功能。

![Harbor Panel 主机概览界面示意图，画面中的监控数据为虚构示例](docs/images/dashboard-preview.svg)

*主机概览界面示意，展示资源趋势与常用管理入口；数据仅用于界面展示。*

## 已实现

| 工作区 | 实际能力 |
| --- | --- |
| 主机概览 | 真实 CPU、物理内存、文件根目录所在卷磁盘、全网卡吞吐、开机时间；2 秒采样，最多保留最近 90 点 |
| 文件管理 | 浏览目录、新建目录、新建/编辑/上传 UTF-8 文本、下载文件；哈希版本冲突检查、编辑时原子替换 |
| Docker | 读取当前 Docker context 的容器列表，启动/停止/重启、读取日志并编辑配置；重建失败时自动尝试恢复旧容器 |
| 诊断终端 | 原生 VPS 部署提供带超时与输出上限的 root Shell；其他部署只提供预设只读诊断 |
| 进程监控 | 真实 PID、进程名称、所属用户、状态、RSS；按内存降序显示前 150 个可访问进程 |
| 操作审计 | 登录、退出、文件变更、诊断和容器操作的请求及结果；SQLite 保留最近 10,000 条 |
| 入口保护 | 密码散列验证、登录限速、服务端会话、HttpOnly/SameSite Cookie、Origin/Host 校验、CSRF、CSP |

界面支持中文、窄屏导航、功能搜索（Ctrl/Cmd K）、编辑器保存快捷键（Ctrl/Cmd S）。所有图表来自实际采样；Docker 不可用时明确显示错误，不填充演示数据。

## 快速启动

要求 Python 3.10 或以上。在仓库根目录执行：

```powershell
cd harbor-panel
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

Linux / macOS：

```sh
cd harbor-panel
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

首次启动需要使用一次性 Base64 凭证注册管理员用户名和密码，并再次输入密码确认。注册后凭证立即失效，账户保存在状态目录中，服务重启后仍可使用；浏览器会话在服务重启时失效。

默认只监听回环地址。默认文件管理目录为项目下的 `workspace/`，审计数据库位于 `.state/`；二者均已忽略，不会作为项目源码提交。可以直接将整个 `harbor-panel` 目录复制到其他位置独立运行。VPS 远程访问请显式设置 `--host 0.0.0.0`，并在云安全组和系统防火墙放行服务端口；公网直连使用 `http://<VPS公网IP>:<端口>`。**原生 Linux 服务模板以 root 运行，可完全管理 VPS；公网 HTTP 不加密，密码和登录会话可能被窃听。请限制端口来源，或自行配置 Nginx 与 HTTPS。**

### 自定义目录

PowerShell：

```powershell
.\.venv\Scripts\python.exe server.py --port 8088 --root 'D:\ServerFiles'
```

Linux：

```sh
.venv/bin/python server.py --root /srv/harbor-workspace --state-dir /var/lib/harbor-panel
```

直接启动（非安装器）时，首次启动会在状态目录自动创建权限受限的 `bootstrap.token`，但不会打印凭证；请从该文件安全取出凭证。使用 Linux 管理菜单时，安装成功后会输出一次性凭证。

## Docker Compose 部署

此方案面向 **Linux Docker Engine + Docker Compose V2**，本地构建镜像，无需在宿主机安装 Python。Docker Desktop 需使用 Linux 容器，并先启用 host 网络支持（要求 4.34 或以上）；监控视图对应其 Linux 运行环境。

克隆仓库后，在含 `compose.yaml` 的项目目录执行：

```sh
docker compose up -d --build
docker compose logs --tail=30 panel
```

首次启动时，容器会在持久化状态卷中生成随机 Base64 凭证并将其输出到容器日志一次。查看 `docker compose logs panel` 并安全保存凭证；注册后凭证文件会删除。此凭证只能使用一次。

服务使用 host 网络并仅监听 `127.0.0.1:8088`，不需要配置 `ports`；确保该端口未被其他程序占用。远程 Linux 服务器可通过 SSH 隧道访问：

```sh
ssh -L 8088:127.0.0.1:8088 user@your-server
```

如需 HTTPS 域名访问，在 `compose.yaml` 的 `panel` 服务中增加以下 `command`，替换为实际域名，再按下方 Nginx 示例配置宿主机反向代理：

```yaml
command: ["python", "server.py", "--root", "/data/workspace", "--state-dir", "/data/state", "--public-origin", "https://panel.example.com"]
```

修改后执行 `docker compose up -d`。使用域名模式后只能通过指定的 HTTPS 地址登录，HTTP 入口不再接受登录请求。容器健康检查仅探测本机监听端口，不代替登录或业务检查。

### 数据与日常操作

- `workspace` 数据卷挂载到 `/data/workspace`，保存面板管理的文件；默认不能浏览宿主机其他目录。
- `state` 数据卷挂载到 `/data/state`，保存 SQLite 审计记录；容器重建后记录保留，登录会话失效。
- 镜像以 UID/GID `10001:10001` 的普通用户运行，应用目录只读。若改用宿主机目录挂载，需要提前为该用户准备可写目录。
- 构建上下文使用白名单，只包含应用源码、依赖清单与静态资源，不包含运行目录、密码文件或缓存。

```sh
docker compose ps
docker compose logs -f --tail=50 panel
docker compose restart panel
docker compose up -d --build
docker compose down
```

依次用于查看状态、跟踪日志、重启、更新本地源码后重新构建、停止服务。`down` 默认保留数据卷；附加 `-v` 会删除文件和审计数据，只有确定要清空数据时才使用。

### 容器模式的功能范围

监控和诊断读取的是容器可见的数据：进程列表属于容器，网络视图受 host 网络影响，CPU、内存和开机时间可能来自宿主内核，不保证与容器配额一致。此方式不提供完整宿主机隔离监控；需要宿主机运维视图时使用下方原生部署。

默认镜像不安装 Docker CLI，也不挂载 Docker socket，因此面板中的 Docker 工作区会明确显示不可用。需要管理宿主机容器时建议使用原生部署；仅添加 socket 挂载不能补齐 CLI，且会授予接近主机管理员的权限。

## Linux 部署

需要直接监控和管理宿主机时，推荐使用原生部署方式。应用只依赖 Python 3.10+ 和 `psutil`，不绑定 Debian、Ubuntu、Alpine 或 CentOS 的专用系统 API。

### 发行版安装

仓库提供 POSIX `sh` 安装脚本，会读取 `/etc/os-release` 并选择对应的软件包管理器：

| 系统 | 包管理器 | 服务管理器 |
| --- | --- | --- |
| Debian / Ubuntu / Linux Mint | `apt` | systemd |
| Alpine Linux | `apk` | OpenRC |
| CentOS / Rocky / AlmaLinux / RHEL / Fedora | `dnf` 或 `yum` | 通常为 systemd |

也可以直接通过 `curl` 打开交互式管理菜单。菜单提供安装、更新、卸载和退出；源码从本仓库下载，更新不会删除工作区、审计数据库或密码配置：

```sh
curl -fsSL https://raw.githubusercontent.com/Jeromeliaya/harbor-panel/main/deploy/menu.sh -o /tmp/harbor-panel-menu.sh
sudo sh /tmp/harbor-panel-menu.sh
```

菜单选项：`1` 安装、`2` 更新、`3` 卸载、`0` 退出。选择安装时会提示输入服务端口，允许 `0-65535`，直接回车使用默认端口 `8088`；端口配置保存于 `/etc/harbor-panel.port`，后续更新会自动沿用。自定义程序和状态目录保存于 `/etc/harbor-panel.paths`，后续菜单操作会继续使用。端口 `0` 由操作系统临时分配，适合测试，不适合作为固定访问地址。卸载需要输入 `UNINSTALL` 确认，清除管理员账户和注册凭证以使其失效，保留 `/srv/harbor-workspace`、审计数据及 `/etc/harbor-panel.env`；重新安装会生成新的注册凭证。更新不会重置账户或凭证。如服务器没有 `curl`，可使用 `wget` 替代下载菜单脚本。

源码默认放在 `/opt/harbor-panel` 时，在项目目录执行：

```sh
sudo sh deploy/install.sh
```

如果源码位于其他目录，设置 `HARBOR_PANEL_DIR`；需要自定义状态目录时可设置 `HARBOR_PANEL_STATE`。安装器会将实际路径保存到 `/etc/harbor-panel.paths`，systemd/OpenRC 服务会自动读取。原生服务始终以 root 运行，不支持切换为受限服务用户。脚本不会覆盖已有的 `/etc/harbor-panel.env`、工作区或审计数据库：

```sh
sudo HARBOR_PANEL_DIR=/srv/harbor-panel sh deploy/install.sh
```

脚本会安装 Python、pip、虚拟环境组件和 `psutil` 所需的构建依赖。原生服务以 `root` 运行，使管理员能够管理宿主机文件、Docker 和系统命令。为避免 root 执行普通用户可修改的程序代码，安装器会将专用程序目录内的常规文件和目录归属迁移到 root，并移除组/其他用户的写权限；专用状态目录设为 root 独占，并将已知认证、审计和凭证文件设为 root 可读写。它不会修改 `/` 或独立工作区的所有权。请勿将用户数据或共享工作区放进程序目录。交互式管理菜单会生成权限为 `0600` 的一次性注册凭证文件，并在安装完成后只输出一次；请立即安全保存。更新保留管理员账户，完成注册后不能再次注册；卸载会使旧凭证和管理员账户失效，重新安装时生成新凭证。安装器会拒绝把程序或状态目录设为 `/`、系统关键目录或共享数据目录。

Debian/Ubuntu、CentOS/RHEL/Fedora 使用 `deploy/harbor-panel.service`；Alpine 使用 `deploy/harbor-panel.openrc`。管理菜单和安装器都会把最终路径写入 `/etc/harbor-panel.paths`，systemd/OpenRC 服务会自动读取；手动部署时复制对应服务模板并启用即可。程序目录中的虚拟环境需位于 `.venv`。

1. 将此目录放到 `/opt/harbor-panel` 并安装虚拟环境依赖。
2. 准备程序目录 `/opt/harbor-panel` 和专用状态目录 `/var/lib/harbor-panel`；原生服务以 root 运行，VPS 其余文件可通过面板管理。安装器会将专用程序目录中的常规文件/目录迁移为 root 所有，旧版独立工作区 `/srv/harbor-workspace` 不会被更新或卸载删除。
3. 使用菜单安装以生成并展示一次性 Base64 凭证；手动部署请将随机凭证保存在状态目录中的 `bootstrap.token`（权限 `0600`），并在启动命令加入 `--bootstrap-token-file /var/lib/harbor-panel/bootstrap.token`。
4. 将对应的 systemd 或 OpenRC 服务模板复制到系统服务目录，确认文件路径和账户正确，再启用服务。
5. 菜单安装会自动尝试显示 VPS 公网 IP，访问 `http://<VPS公网IP>:<所选端口>` 并完成首次注册；若 IP 无法自动探测，请从 VPS 服务商控制台获取。确认云安全组及系统防火墙允许该 TCP 端口入站。手动部署使用公网访问时，在启动命令加入 `--host 0.0.0.0`；若只希望通过 SSH 隧道访问，则继续使用 `ssh -L 8088:127.0.0.1:8088 user@your-server`，然后访问本机 `http://127.0.0.1:8088`。

如需使用域名，可选配置 Nginx 与 HTTPS，并在 service 的 `ExecStart` 末尾添加 `--public-origin https://panel.example.com`。外部 Origin 必须精确匹配 HTTPS 协议、主机及非默认端口；该模式设置 Secure Cookie，浏览器登录必须通过指定 HTTPS 域名。配置该模式后，原有公网 IP:端口 HTTP 登录会被拒绝；不配置 `--public-origin` 时则可继续通过公网 IP 和端口访问，但必须接受未加密 HTTP 风险。反向代理要保留 Host，例如 TLS Nginx server 内配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:8088;
    proxy_set_header Host $http_host;
    proxy_read_timeout 60s;
    client_max_body_size 2m;
}
```

示例后端端口为 `8088`。建议同时用 VPS 防火墙限制该端口，只允许可信来源或本机代理。后端不信任 `X-Forwarded-For`，代理部署下审计来源及登录限速按代理地址统计。这里使用 Python 标准库 HTTP 服务，面向单管理员低并发场景，不建议作为直接暴露公网的大规模控制面。

### Docker 管理

原生 Linux systemd/OpenRC 服务以 root 运行，可访问标准 Docker socket；Docker CLI 和 daemon 仍需由用户在 VPS 上安装并启动。若容器页仍报权限错误，先确认 Docker daemon 和 CLI 可用，再检查 systemd 是否有覆盖服务用户/文件系统权限的 drop-in：`systemctl cat harbor-panel` 和 `systemctl show -p User -p DynamicUser -p ProtectSystem harbor-panel`。**不要盲目删除现有 drop-in**；只调整你确认属于 Harbor Panel、且确实限制权限的设置。若面板信息页未显示“全主机 root”，说明当前进程没有实际以 root 运行。Compose 容器模式不挂载宿主机 Docker socket，仍无法管理宿主机 Docker。

### 权限与边界

- 原生 Linux 部署的文件 API 可浏览宿主机 `/`，但隐藏并拒绝访问面板认证、凭证和审计状态目录；不支持设备名、NTFS 数据流及非便携文件名。符号链接只在原生全主机模式下跟随，目标仍须位于根目录内。
- 文件编辑/上传限 256 KiB 且仅支持 UTF-8，下载限 8 MiB；文件 API 不提供删除/重命名。原生 root Shell 可执行系统管理命令，受信任管理员能通过 Shell 完整管理文件、服务、进程及 Docker。
- 原生 root Shell 每条命令最多运行 30 秒，输出上限 1 MiB；命令在服务进程的当前工作目录执行（原生 systemd/OpenRC 模板设为 `/`）。命令仍以登录会话、Origin、CSRF 和审计保护。**Root Shell 可修改或删除系统文件、破坏系统或数据，公网 HTTP 还会暴露口令和会话。**只能授予可信管理员，并通过防火墙限流或配置 HTTPS 反向代理。面板不是多租户安全边界。
- 除原生 Linux 全主机部署外，手动启动仍按当前操作系统账户与指定文件根目录运行；Docker 需要可用的 CLI 与 daemon。
- 手动/Compose 模式的 `help`、`hostname`、`whoami`、`uptime`、`df -h`、`free -h`、`ps`、`docker ps` 是预设诊断，不提供 Shell；原生全主机模式开放 root Shell。
- 主机历史暂存在内存，服务重启后清空。审计保存在本地 SQLite，既不是防篡改存储，也不是集中日志系统。
- 尚未实现远程节点、网站/证书、服务配置、应用市场、备份调度、AI 助手与桌面窗口系统。

## 开发与测试

在本目录执行：

```sh
python -m unittest discover -s tests -v
node --check web/app.js
```

测试使用临时目录和真实 HTTP 服务，覆盖登录/会话/CSRF、目录越界、文件冲突、真实监控与诊断、审计、Docker 参数验证与输出限制。Docker 生命周期测试使用 mock，不启停本机真实容器。Node 仅用于可选的前端语法检查，正常运行不需要安装。

```text
harbor-panel/
  server.py       HTTP、认证、路由、审计、启动入口
  host.py         主机采样、进程、Docker、诊断
  files.py        目录约束与文件读写
  web/            原创中文管理界面，无构建步骤
  tests/          标准库 unittest 测试
  deploy/         Linux systemd 示例
  deploy/install.sh       Debian/Ubuntu/Alpine/CentOS 安装脚本
  deploy/harbor-panel.openrc  Alpine OpenRC 服务示例
  deploy/menu.sh          curl 交互式安装/更新/卸载菜单
  Dockerfile      非 root 容器镜像
  compose.yaml    Compose 启动与持久化配置
  .env.example    可选固定密码配置模板
  requirements.txt
```

## 许可

本项目采用 MIT 许可，详见本目录 `LICENSE`。
