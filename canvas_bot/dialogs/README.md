# `dialogs`：模态框优先处理

`dispatch_modal` 是模态框唯一入口。模态框覆盖底层页面时，必须优先处理模态框，否则底层坐标点击可能提交、取消或跳转到错误页面。

## 已支持的弹窗

| 页面标识 | 委托动作 | 业务边界 |
| --- | --- | --- |
| `system_ignore_dialog` | 点击“忽略” | 系统标题与“忽略”必须同屏；优先于任意底层页面和表单等待 |
| `system_notice_dialog` | 关闭安全通知 | 无“忽略”时仅关闭已识别通知的右上角关闭图标 |
| `submit_confirm_dialog` | 点击变现二次确认的“确认” | 仅在本轮已提交变现表单的短时窗口内执行 |
| `trade_success_dialog` | 点击“知道了”关闭成功提示 | 必须同时识别“交易成功”和“知道了”，不触碰到账或放行确认 |
| `trade_notification` | `handle_incoming_order` | 仅在自动接单开关开启且收款方式匹配时同意 |
| `order_exception_dialog` | `handle_order_exception_dialog` | 按既有规则忽略/关闭异常提示 |
| `cancel_confirm_dialog` | `handle_cancel_confirm_dialog` | 只确认当前已选择的超时挂单 |
| `password_dialog` | `handle_runtime_password_dialog` | 仅使用控制台内存中的支付密码；输入后弹窗未关闭时不重复输入 |

## 修改规则

- 新弹窗先在页面识别逻辑中命名，再在本映射添加条目。
- 处理器必须只消费 `PageContext` 当前扫描结果，不得猜测底层页面状态。
- 支付密码、确认取消、确认变现属于不可逆动作；不得添加“识别失败时仍点击”的兜底。
- 未识别的弹窗返回 `False`，交由主循环的未知页策略处理，不应点击屏幕空白区域。
