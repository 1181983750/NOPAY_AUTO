# `pages`：非模态页面路由

`handlers.py` 只处理已经识别出的**普通页面**。页面上若存在模态框，必须先由 `dialogs/` 处理，不能在这里直接点击底层按钮。

## 页面与唯一职责

| 页面标识 | 处理器 | 允许的动作 |
| --- | --- | --- |
| `auth_page` | `handle_auth_page` | 提示用户手动登录，不刷新、不输入账号密码 |
| `navigation` | `handle_navigation_page` | 进入搬砖页 |
| `move_page` | `handle_move_page` | 交给订单工作流检查余额、订单和挂单状态 |
| `withdraw_form` | `handle_withdraw_form_page` | 交给订单工作流填写并提交变现表单 |
| `withdraw_unavailable` | `handle_withdraw_unavailable_page` | 处理余额不足的变现页 |
| `orders_list` / `orders_empty` | `handle_orders_page` | 检查在途订单、取消超时订单或返回搬砖页 |
| `order_detail` | `handle_order_detail_page` | 返回订单列表，避免详情页阻塞流程 |
| `waiting_transfer` / `waiting_transfer_detail` | `handle_waiting_page` | 记录等待状态；通知由分发器去重发送 |
| `unknown` | `handle_unknown_page` | 使用已有冷却和刷新策略，不做额外点击 |

## 固定执行顺序

`dispatch_page` 必须保持以下顺序：

1. `unknown` 页面直接走未知页冷却策略。
2. 待付款页仅发送去重后的“待付款”通知。
3. 全局检查“接单后订单状态”和“新交易通知”。
4. 分发到当前页面的唯一处理器。
5. 仅在刚刚发起跳转的加载窗口内记录“等待变现页面加载”。

## 修改规则

- 新页面必须新增独立 `handle_<page>_page` 函数，再加入映射；不要在现有处理器中混入别的页面。
- 页面处理器只做路由和页面专属意图，复杂状态迁移放到 `workflows/`。
- 不得绕过 `CanvasBot` 的视觉确认、操作节流和金额校验。
- 不得在此恢复“脚本启动”“收到交易”通知；当前产品策略仅推送待付款。
