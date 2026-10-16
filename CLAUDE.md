# CLAUDE.md

本仓库是 TauricResearch/TradingAgents 的个人改造版（分支 `my-custom`）：增加了 A 股 / 港股数据、多源数据网关、仓位风控、模拟盘、选股，以及一个多账户 Web 服务。个人环境与协作约定见 `CLAUDE.local.md`（不提交）。

## 目录

- `tradingagents/`：核心库
  - `agents/`：分析师、多空研究员、风格研究员（`researchers/style_panel.py`）、交易员、风控、经理
  - `dataflows/`：数据层；`vendors/akshare_cn.py`（A 股 / 港股）、`vendors/yahoo/`、`router.py`（数据源链）
  - `datagw/`：统一数据网关（Tushare → 新浪 → 东方财富，按类型多源回退、熔断、质量校验）
  - `graph/`：LangGraph 流程、分析师报告复用（`report_cache.py`）
  - `memory/`：决策日志、结算、反思与“学到的规则”
  - `screener.py` / `scoring.py` / `review.py`：选股、六因子打分、快速复核
  - `risk.py` / `paper.py` / `vote.py` / `usage.py`：仓位风控、模拟盘、多次投票、费用统计
- `server/`：FastAPI 服务（`app.py` 接口、`runner.py` / `worker.py` 任务、`scheduler.py` 定时、`services/` 按用户的业务逻辑、`static/` Vue 3 前端，vendor 本地化不走 CDN）
- `cli/`：命令行（`tradingagents ...`）
- `scripts/`：运维与诊断脚本（`run_server.ps1`、`run_daily.ps1`、`check_tushare.py`、`check_cn_data.py` 等）
- `docs/deploy.md`：常驻运行、多终端访问、备份

## 常用命令

```
pip install -e ".[server,cn,dev]"
python -m pytest -q -p no:cacheprovider        # 完整测试
tradingagents server selfcheck                 # 替身模型跑完整分析 + 比对运行中服务的代码指纹
tradingagents server run                       # 启动服务（默认 127.0.0.1:8000，接口文档 /docs）
tradingagents server service install           # 安装常驻服务（Windows 登录后后台运行）
tradingagents datasource status | check        # 数据源状态 / 逐个实测（不调模型）
```

## 规则

- **密钥**：不读、不打印、不提交 `.env`；页面和日志不显示任何 Key 或 token。
- **测试不联网**：`tests/conftest.py` 拦截对外连接（只放行 127.0.0.1 / ::1）；必须联网的测试标 `integration`。测试的结果、缓存、记忆日志都放在临时目录，不碰用户目录下的真实数据。
- **测试要在 Windows 和 Linux 上都通过**：按字节读的测试文件用 `write_bytes`（Windows 的 `write_text` 会写成 CRLF）；不要假设临时目录不在用户目录下。
- **分层**：yfinance 等数据源库只能在 `tradingagents/dataflows/` 里导入（`tests/test_layering.py` 检查）。
- **股票代码先统一写法再比较**：`.SH` / `SH603980` / `603980` 统一成 `603980.SS`（用 `normalize_symbol`），否则会被当成两只股票。
- **点位时间**：分析和回测只能用决策日当时已公开的数据；新接入的数据要给出公告/发布日期规则。
- **数字交给代码**：估值、仓位、止损、费用等用确定性代码计算，模型只做解读。
- **新线程要重新包 `run_config`**（它是 contextvars）。
- **数据库升级**：`create_all` 不改旧表；新增列写在 `server/db.py` 的 `_ADDED_COLUMNS`。重建表时先把旧表改名再复制，最后才删旧表（SQLite 驱动会自动提交 DROP，回滚救不回数据）。
- **前端缓存**：页面文件带 `Cache-Control: no-cache`；改完前端仍看到旧页面时先 Ctrl+F5。
- **服务**：只在普通（非管理员）窗口安装和重启；配置（含 `.env`）只在启动时读取，改了要重启。
- **换行**：仓库在 Windows 上以 CRLF 检出，写回文件保持原有换行。
- **用户自己提交和推送**；不要替用户 commit / push。
