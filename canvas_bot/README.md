# `canvas_bot` 模块边界

这是自动化脚本的运行包。当前的 `CanvasBot` 仍位于 `__main__.py`，负责浏览器会话、OCR、运行状态和底层业务动作；页面识别后的路由已拆到子模块中。

## 总体调用顺序

1. `CanvasBot` 截图并用 OCR 识别页面类型。
2. 优先交给 `dialogs/` 处理模态框。
3. 非模态页面交给 `pages/` 分发。
4. `pages/` 在需要跨页面状态迁移时调用 `workflows/`。
5. 所有页面操作通过 `PageContext` 使用同一次扫描结果，避免重复截图后坐标失效。

## 修改规则

- **页面新增或页面跳转**：只在 `pages/` 增加页面处理器和路由映射，不要在 tick 主循环堆叠 `if/else`。
- **弹窗新增**：只在 `dialogs/` 增加映射，并确认其优先级高于底层页面。
- **挂单、取消、接单等跨页面流程**：放在 `workflows/`；实际点击和字段校验仍复用 `CanvasBot` 已有方法。
- **纯数据结构、文本处理**：放在 `core/`，不得依赖浏览器或 `CanvasBot`。
- **第三方通知或网络服务**：放在 `services/`，不得包含页面点击逻辑。

不要因为代码位置移动而改变：金额校验、支付密码输入、取消超时、收款方式匹配、自动接单开关、等待付款通知策略。任何涉及这些规则的修改都必须补测试并人工核对日志。

## 验证

```powershell
uv run python -m compileall -q canvas_bot
uv run python -m unittest discover -s tests -q
```

页面职责详见 [`../docs/页面状态机.md`](../docs/页面状态机.md)。
