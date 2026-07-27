# NO 钱包 Canvas 自动化

面向 Flutter/canvas 页面的一套本地浏览器自动化工具。脚本通过 Playwright 控制独立 Chrome、截取应用区域并以 OCR/模板识别页面状态，再执行已限定的挂单、接单、订单跟踪和超时重挂动作。

涉及“确认到账”“放行”等资金安全确认的操作被明确拦截，不会自动点击。

## 快速开始

环境要求：Windows、Python 3.10–3.12、[uv](https://docs.astral.sh/uv/)、Google Chrome。

```powershell
uv sync --extra ocr --link-mode=copy
uv run qjd67-canvas-bot doctor --config configs/qjd67.json
uv run qjd67-canvas-bot dashboard --config configs/qjd67.json
```

打开 <http://127.0.0.1:8765>，首次运行时在自动化浏览器中手动登录，再从本地控制台启动脚本。

## 项目结构

```text
nopay/
├── canvas_bot/                 # 自动化主包
│   ├── __main__.py             # CLI、浏览器会话、OCR、状态机与诊断监控
│   ├── dashboard.py            # 仅绑定本机的控制台与进程管理
│   ├── core/                   # 数据模型、页面上下文、文本与路径工具
│   ├── dialogs/                # 高优先级模态框路由
│   ├── pages/                  # 普通页面路由
│   ├── workflows/              # 跨页面订单流程
│   └── services/               # PushPlus、Telegram 等外部服务
├── configs/
│   └── qjd67.json              # 当前运行配置（请勿提交密码）
├── docs/                       # 使用、模块和状态机详细文档
├── tests/                      # 单元测试
├── debug-screenshots/          # 当前一轮的日志与调试截图（Git 忽略）
├── pyproject.toml              # Python 依赖与 CLI 入口
└── uv.lock                     # 锁定依赖版本
```

`.browser-profile/` 是独立 Chrome 登录状态，`.venv/` 是 uv 创建的虚拟环境；两者均为本机运行数据，不应提交。

## 常用命令

| 目标 | 命令 |
|---|---|
| 检查依赖与配置 | `uv run qjd67-canvas-bot doctor --config configs/qjd67.json` |
| 手动登录并保存 Cookie | `uv run qjd67-canvas-bot login --config configs/qjd67.json` |
| 截图并输出 OCR 结果 | `uv run qjd67-canvas-bot snapshot --config configs/qjd67.json` |
| 直接运行自动化 | `uv run qjd67-canvas-bot run --config configs/qjd67.json` |
| 启动本地控制台 | `uv run qjd67-canvas-bot dashboard --config configs/qjd67.json` |
| 运行测试 | `uv run python -m unittest discover -s tests -v` |

优先使用本地控制台启动和停止脚本。它只管理自身创建的自动化进程，不会自动取消平台上已经存在的订单。

## 配置要点

主配置是 [`configs/qjd67.json`](configs/qjd67.json)。常用项如下：

| 配置区 | 用途 |
|---|---|
| `browser_profile` / `user_data_dir` | 默认使用独立 managed Chrome Profile，避免占用日常 Chrome |
| `amount`、`limit_min`、`limit_max` | 挂单金额与单笔限额 |
| `payment.keywords` | 收款方式的 OCR 匹配关键词，按顺序轮换 |
| `auto_make_order`、`auto_repost`、`auto_accept` | 自动挂单、重挂、接单开关 |
| `multi_order` | 多挂单、启动后接管已有订单、取消扫描边界 |
| `speed_mode` | `safe` 或 `fast`；只调整可逆的页面/表单等待 |
| `debug` | 操作截图与 OCR 标注保存策略 |
| `performance_monitor` | 每 60 秒记录耗时、OCR 与 Python/Chrome 内存 |
| `diagnostics` | 未知页面与异常现场截图的保存阈值和限频 |

支付密码不要写入 JSON。推荐在本地控制台中临时输入（只保存在该控制台内存），或设置当前会话环境变量：

```powershell
$env:QJD67_PAY_PASSWORD = "六位数字密码"
```

保存控制台配置后，需要停止并重新启动脚本才会加载新参数。

## 排障入口

每次由控制台启动都会清理上一轮调试截图，并在 `debug-screenshots/` 留下本轮材料：

| 文件或日志标记 | 用途 |
|---|---|
| `dashboard-bot.log` | 本轮完整运行日志 |
| `*_unknown_page.png` | 未知页面连续达到诊断阈值时的现场 |
| `*_tick_error.png` | 运行异常时的现场，默认每 30 秒至多一张 |
| `[perf]` | 单轮、截图、OCR 耗时，以及 Python/独立 Chrome 内存 |
| `[boot]` | 本轮速度、刷新和诊断阈值摘要 |

示例性能日志：

```text
[perf] tick=2.31s screenshot=0.04s ocr=1.65s python_rss=382MB managed_chrome_rss=534MB
```

如果 `ocr` 或 `tick` 持续升高，先检查窗口尺寸、页面内容和调试截图；如果 Python 或 `managed_chrome_rss` 持续单向增长，再将同一轮的性能日志与异常截图一并保留以便排查。截图和日志可能包含订单或收款信息，分享前务必脱敏。

## 详细文档

- [使用与启动说明](docs/使用与启动说明.md)
- [功能模块说明](docs/功能模块说明.md)
- [页面状态机与操作边界](docs/页面状态机.md)
- [`canvas_bot` 包内模块边界](canvas_bot/README.md)

## 安全边界

- 不自动登录、注册或处理验证码。
- 关键金额、限额、收款方式与页面状态需通过 OCR/模板回读确认。
- 未识别页面只等待并按阈值刷新，不进行猜测性点击。
- 不自动确认到账、放行或任何危险资金操作。
- 支付密码、通知 Token、登录 Profile、日志与截图均不得提交到 Git。
