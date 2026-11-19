# Harbor Panel

从零编写的轻量单机运维面板。采用 **Python + 原生 JavaScript/CSS**，无需 Node 构建工具，只有一个 Python 依赖 `psutil`。可在 Windows 本地运行，也可原生部署在 Linux 上。

这是受 KPanel 产品方向启发的独立实现，使用原创代码、界面与品牌，不依赖或包含 KPanel 源码、Logo、脚本或资源。不代表已实现 KPanel 的全部功能。

## 已实现

| 工作区 | 实际能力 |
| --- | --- |
| 主机概览 | 真实 CPU、物理内存、文件根目录所在卷磁盘、全网卡吞吐、开机时间；2 秒采样，最多保留最近 90 点 |
| 文件管理 | 浏览目录、新建目录、新建/编辑/上传 UTF-8 文本、下载文件；哈希版本冲突检查、编辑时原子替换 |
| Docker | 读取当前 Docker context 的容器列表，启动/停止/重启，读取最近 150 行日志；操作前确认 |
| 诊断终端 | 预设只读命令、历史记录、结构化执行；不调用任意 Shell |
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

打开终端显示的 `http://127.0.0.1:8088`，用终端输出的临时密码登录。未配置固定密码时，每次启动生成新密码；所有浏览器会话在服务重启时失效。

默认只监听回环地址。默认文件管理目录为项目下的 `workspace/`，审计数据库位于 `.state/`；二者均已忽略，不会作为项目源码提交。可以直接将整个 `harbor-panel` 目录复制到其他位置独立运行。

### 固定密码与自定义目录

PowerShell：

```powershell
$env:HARBOR_PASSWORD = 'replace-with-a-long-unique-password'
.\.venv\Scripts\python.exe server.py --port 8088 --root 'D:\ServerFiles'
```

Linux：

```sh
export HARBOR_PASSWORD='replace-with-a-long-unique-password'
.venv/bin/python server.py --root /srv/harbor-workspace --state-dir /var/lib/harbor-panel
```

密码至少 12 字符。示例密码必须替换；服务不保存明文密码。临时生成的密码仅输出到启动终端，避免将启动输出公开分享。

## Linux 部署

推荐原生部署，使监控和文件视图对应真实宿主机。容器化运行时这些指标会对应容器环境，因此此版本不提供容易混淆宿主机权限的 Docker 部署脚本。

1. 将此目录放到 `/opt/harbor-panel` 并安装虚拟环境依赖。
2. 创建专用普通用户 `harbor`、其可读写的 `/srv/harbor-workspace` 与 `/var/lib/harbor-panel`。
3. 创建 `/etc/harbor-panel.env`，写入 `HARBOR_PASSWORD=你的强密码`，权限设置为 `0600`，由 root 所有。
4. 将 `deploy/harbor-panel.service` 复制到 `/etc/systemd/system/`，确认文件路径和账户正确，再执行 `systemctl daemon-reload`、`systemctl enable --now harbor-panel`。
5. 本地使用 SSH 隧道：`ssh -L 8088:127.0.0.1:8088 user@your-server`，然后访问本机 `http://127.0.0.1:8088`。

如需使用域名，在 HTTPS 反向代理后运行，并在 service 的 `ExecStart` 末尾添加 `--public-origin https://panel.example.com`。外部 Origin 必须精确匹配协议、主机及非默认端口；该模式设置 Secure Cookie，必须使用 HTTPS 浏览器入口。反向代理要保留 Host，例如已有 TLS Nginx server 内配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:8088;
    proxy_set_header Host $http_host;
    proxy_read_timeout 25s;
    client_max_body_size 2m;
}
```

保持后端回环监听；通过防火墙限制管理入口。后端不信任 `X-Forwarded-For`，代理部署下审计来源及登录限速按代理地址统计。这里使用 Python 标准库 HTTP 服务，面向单管理员低并发场景，不建议作为直接暴露公网的大规模控制面。

### 权限与边界

- 文件根目录之外不可通过文件 API 访问；不跟随符号链接或 Windows junction，不支持保留设备名、NTFS 数据流及非便携文件名。
- 文件编辑/上传限 256 KiB，必须是有效 UTF-8；下载限 8 MiB。不提供删除文件或结束进程操作。
- 管理目录应由面板账户控制，**不要允许不可信本地账户并发改动目录结构**。路径检查不能替代操作系统隔离；这个目录限制不是抵御恶意本地进程的安全沙箱。
- 文件保存使用内容哈希检查外部变更；检查与替换之间仍不构成跨进程文件锁，不适合多个不受控写入者同时改同一配置。
- 面板按当前操作系统账户权限运行，不自动提权。不要为了方便给它 root 权限。
- Docker 需要可用的 CLI 与 daemon。Docker socket 权限接近主机管理员权限，应仅在信任的管理员环境授予；systemd 示例默认不添加 docker 组。
- `help`、`hostname`、`whoami`、`uptime`、`df -h`、`free -h`、`ps`、`docker ps` 是可用诊断。它们是跨平台结构化诊断，并不提供 SSH / PTY / Shell 兼容性。
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
  requirements.txt
```

## 许可

本项目遵循所在仓库的 MIT 许可，见本目录 `LICENSE`。与 KPanel 项目不存在从属或官方合作关系。
