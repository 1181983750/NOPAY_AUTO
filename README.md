# QJD67 Canvas Browser Bot

这个版本不再依赖页面 DOM 文字。它用 Python + Playwright 控制 Chrome，对 Flutter/canvas 区域截图，再通过 OCR、模板图或比例坐标决定点击哪里。

## 安装

```powershell
uv sync
```

如果要识别 canvas 里的中文文字，安装 OCR 依赖：

```powershell
uv sync --extra ocr
```

第一次运行如果 Playwright 找不到浏览器，可以安装浏览器，或者继续使用配置里的本机 Chrome：

```powershell
uv run playwright install chromium
```

## 配置

编辑 `configs/qjd67.json`：

- `amount`：变现金额，必须填写。
- `limit_min` / `limit_max`：单笔限额，可留空。
- `payment.keyword`：OCR 找收款方式时用的关键词。
- `payment.card_index`：保留给旧配置兼容；当前安全模式不会按卡片序号盲点。
- 支付密码不要写进配置，推荐放环境变量：

```powershell
$env:QJD67_PAY_PASSWORD="你的支付密码"
```

## 运行

先做环境检查：

```powershell
uv run qjd67-canvas-bot doctor --config configs/qjd67.json
```

第一次先手动登录。浏览器打开后登录，确认进入首页，再回到终端按 Enter：

```powershell
uv run qjd67-canvas-bot login --config configs/qjd67.json
```

打开浏览器并截一张 canvas 调试图：

```powershell
uv run qjd67-canvas-bot snapshot --config configs/qjd67.json
```

确认坐标和截图没问题后启动：

```powershell
uv run qjd67-canvas-bot run --config configs/qjd67.json
```

## 本地控制台

启动本机网页控制台：

```powershell
uv run qjd67-canvas-bot dashboard --config configs/qjd67.json
```

浏览器打开 `http://127.0.0.1:8765` 后可调整金额、最小/最大限额、超时重挂、轮询和确认等待参数，也能启动或停止由控制台创建的脚本、查看日志和调试截图。保存参数后需要在控制台停止并重新启动脚本才会生效。

控制台只监听本机地址。支付密码可在控制台中输入，但只保留在当前控制台内存；脚本仅在识别到支付密码弹窗时，通过随机令牌保护的本机接口读取最新值。因此脚本运行中也可先设置或更新密码，不会写入 JSON、日志、Git 或子进程环境变量。关闭控制台后需要重新输入。也可继续使用 `QJD67_PAY_PASSWORD` 环境变量。

默认配置会使用 `.browser-profile/qjd67` 作为独立调试窗口。第一次需要手动登录，之后 cookie 会保留：

```json
"browser_profile": {
  "mode": "managed"
}
```

这个模式不会占用你平时正在用的 Chrome，也不用关闭已有 Chrome 窗口。

默认 `viewport` 为 `null`，调试浏览器会使用 Chrome 的原生窗口尺寸，让目标网站保持与手动打开时相同的响应式布局。脚本仍会每轮识别 Flutter 内容区的位置。若要固定窗口以便复现 OCR 调试，可改回：

```json
"viewport": { "width": 1280, "height": 900 }
```

脚本打开后会先通过 OCR 点击顶部“搬砖”标签，再识别“开始搬砖”按钮；不会在默认的“买卖”页直接开始挂单。

多挂单参数可在配置中设置：

```json
"multi_order": {
  "enabled": true,
  "payment_keywords": ["魔仙", "另一张卡"],
}
```

每次成功挂单后，脚本按 `payment_keywords` 顺序循环选择收款卡。它会 OCR 读取顶部“搬砖余额”，单笔金额不超过当前余额；当余额低于配置的 `limit_min`（最小限额）时停止新增挂单。若余额不足配置的变现金额，脚本点击页面的“全部”填写精确余额。只会对本次运行期间创建并计时的挂单进行超时取消，旧挂单不会被盲目取消。

如果以后想复用你平时 Chrome 里的登录状态，再把 `mode` 改成 `system`：

```json
"browser_profile": {
  "mode": "system",
  "system_user_data_dir": "%LOCALAPPDATA%\\Google\\Chrome\\User Data",
  "profile_directory": "Default"
}
```

`system` 模式运行前需要先关掉所有 Chrome 窗口。Chrome 的用户目录同时只能被一个浏览器进程占用。如果你的常用 Chrome 不是 `Default` profile，在地址栏打开 `chrome://version`，看“个人资料路径”最后一段，比如 `Profile 1`，然后把 `profile_directory` 改成对应值。

取消挂单的二次确认节奏可以在 `cancel_flow` 调：

```json
"cancel_flow": {
  "confirm_delay_sec": 1.2,
  "max_confirm_clicks": 1
}
```

如果页面加载慢，把 `confirm_delay_sec` 调大，比如 `1.8`。识别不到取消确认时脚本不会按坐标猜测，而是保留调试截图等待你检查。

挂单页面会优先用 OCR 按文字选择收款卡片：

```json
"payment": {
  "keyword": "魔仙",
  "ocr_attempts": 8,
  "fallback_to_card_index": false
}
```

脚本会在金额和限额填好后由 Playwright 截图，在“收款方式（可多选）”表单区域先取消已选的默认卡，再匹配 `payment.keyword` 对应的收款卡片文字并点击。目标卡未完全露出时才会在卡片区域小幅下滚；找不到卡片则保存调试截图并暂停本次挂单，不会按 `card_index` 坐标兜底。

确认变现现在分两步：

```json
"submit_flow": {
  "secondary_confirm_delay_sec": 1.0,
  "secondary_confirm_timeout_sec": 7.0,
  "password_timeout_sec": 8.0,
  "return_to_start_timeout_sec": 8.0
}
```

先识别并点击最底部的“确认变现/确定变现”，再等待 OCR 或模板检测到底部二次确认，确认后等待检测到支付密码弹窗，最后输入支付密码。输完后会等待检测到“开始搬砖”页面，才认为挂单完成。任一状态未出现都会保存截图并暂停当前挂单，不会用坐标猜测点击。

支付密码默认按六位自动提交处理，输完后不会再点击确认：

```json
"password": {
  "post_input_wait_sec": 0.8,
  "confirm_after_input": false
}
```

如果某个页面版本输完密码后还需要点确认，再把 `confirm_after_input` 改成 `true`。

## 模板识别

OCR 不稳定时，可以把小截图放到 `templates/`：

```text
templates/
  pending.png
  accept.png
  order_form.png
```

脚本会在每轮截图中尝试匹配这些模板。模板越小越清晰越好，比如只截按钮文字或状态标签。

## 安全边界

脚本内置了到账、放行等危险确认词拦截。它可以自动挂单、重挂、同意接单，但不会自动点击类似“确认到账/放行”的危险确认。
