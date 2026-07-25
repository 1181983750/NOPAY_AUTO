# `services`：外部服务适配器

包含 `PushPlusNotifier`（微信）和 `TelegramNotifier`（Telegram Bot API）。服务层只关心“发送什么”，不判断页面、不点击浏览器、不管理订单状态。

## 通知策略（固定）

- **发送**：订单进入 `waiting_transfer` 或 `waiting_transfer_detail` 时，经过 `notify_once` 去重后向所有已配置渠道发送“待付款”。
- **不发送**：脚本启动、收到交易通知、自动同意接单、普通扫描日志。
- 标题和文案不得自动添加“NO钱包”前缀；由调用方传入最终内容。

## 配置与安全

- Token 来源：环境变量 `QJD67_PUSHPLUS_TOKEN`，Windows 下可回退读取当前用户环境变量注册表。
- Telegram 配置：`QJD67_TELEGRAM_BOT_TOKEN` 与 `QJD67_TELEGRAM_CHAT_ID` 两者都存在才启用；同样支持 Windows 当前用户环境变量注册表。
- Token 不得写入仓库、`configs/*.json`、日志、截图或 README 示例。
- 发送失败必须是 best-effort：记录错误但不能中断浏览器自动化。

## 新服务规则

新增邮件等外部通知时，在本目录单独建立适配器；保持与现有服务相同的“无页面逻辑、无敏感信息输出、失败不阻塞主流程”原则。通知触发条件仍由 `pages/` 或上层 `CanvasBot` 决定。
