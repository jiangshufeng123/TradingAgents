# 部署与多终端访问

这份说明对应 `tradingagents server` 系统服务：这台电脑作为服务器常驻运行，
手机、笔记本等其他设备通过 Tailscale 访问。暂不对公网开放；以后要开放时见最后一节。

## 1. 让服务常驻运行

```powershell
tradingagents server service install
```

- 在你的“启动”文件夹（`shell:startup`）放一个启动脚本 `TradingAgents Server.vbs`：
  每次登录 Windows 后在后台（不弹窗口）运行 `scripts\run_server.ps1`，服务意外停止会在
  15 秒后自动重启。不需要管理员权限。
- 日志：`logs\server_<日期>.log`。
- 默认监听 `0.0.0.0:8000`（本机、家里局域网、Tailscale 都能访问，其他来源一律拒绝）。
  只想本机用：`tradingagents server service install --host 127.0.0.1`。
- 查看状态：`tradingagents server service status`；停止并删除：`tradingagents server service remove`。
- 电脑休眠时服务也会暂停：设置 → 系统 → 电源，把“接通电源时进入睡眠”设为“从不”。
- 装成常驻服务后，就不要再手动运行 `tradingagents server run` 了（端口会冲突）。

## 2. 打开防火墙（一次）

用“以管理员身份运行”打开 PowerShell，进入项目目录、激活环境后：

```powershell
tradingagents server firewall
```

只对 Tailscale（100.64.0.0/10）和本机所在局域网放行 8000 端口。只允许 Tailscale：加 `--no-lan`。

## 3. 用 Tailscale 让手机和其他电脑访问

1. 在 https://tailscale.com 注册（可用 Google / Microsoft / GitHub 账号）。
2. 这台电脑安装 Tailscale 并登录；手机在应用商店装 Tailscale，用同一个账号登录。
3. 服务启动时日志里会列出访问地址，形如 `http://100.x.y.z:8000`；
   也可以在 Tailscale 客户端里看到这台电脑的 100.x 地址或机器名。
4. 手机浏览器打开这个地址，登录即可。可“添加到主屏幕”当 App 用。

Tailscale 只连接你授权的设备，流量端到端加密，不需要公网 IP、域名或备案。
给家人用：在 Tailscale 后台把他们的设备共享进来，再在“设置 → 账号管理”里给他们建账号。

服务另有一道地址检查：只接受 `TRADINGAGENTS_ALLOWED_NETS` 里的网段（默认本机、
局域网私有地址、Tailscale）。即使防火墙配错，来自公网的请求也会被拒绝（403）。

## 4. 每日定时分析：从 Windows 计划任务切换到服务

两者只能开一个，否则同一批股票会被分析两次。

```powershell
tradingagents schedule remove
```

然后在网页“设置 → 每日定时分析”里开启，选时间和星期。之后每天按时把自选股排入
分析任务，结果在“分析”页和“今日结论”里，模拟盘按各账号自己的开关下单。

## 5. 备份

- 服务每天 03:00 自动备份数据库到 `~/.tradingagents/backups/`，保留最近 14 份。
- 手动：`tradingagents server backup`。
- 报告、成本账单、模拟账户在报告目录（如 `D:\tradingagents_reports`），请纳入你自己的文件备份。
- 恢复：停止服务，把某个备份复制回 `~/.tradingagents/server.db`，再启动。

## 6. 以后开放公网（暂不需要）

需要时再做，步骤概要：

1. 一个域名，解析到你的公网 IP（家宽通常需动态域名），或借一台云服务器用 frp 中转
   （国内云服务器绑定域名一般需要备案）。
2. 用 Caddy 做反向代理并自动申请 HTTPS 证书，例如 `Caddyfile`：
   ```
   ta.example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```
3. 服务改为只监听本机：`tradingagents server service install --host 127.0.0.1`
   （只有 Caddy 能连到它）。
4. `.env` 设置 `TRADINGAGENTS_COOKIE_SECURE=true`（登录凭证只走 HTTPS）。
5. 所有账号使用强密码；登录失败 5 次会锁定 10 分钟。

多台服务器、PostgreSQL、独立任务进程（Docker Compose）留到用户和任务量上来以后。
