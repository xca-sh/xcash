"""
invoices 模块 — 加密货币支付账单管理。

并发控制策略
============

本模块存在两种并发控制策略，按场景选择：

1. **悲观锁（select_for_update）**——用于需要读-改-写同一行的路径。
   当前账单支付指引直接存放在 Invoice 上，因此这些路径只锁 Invoice。

   - InvoiceService.try_match_invoice() — 无锁探测 → 锁 Invoice → 复核当前支付指引
   - tasks.fallback_invoice_expired() — 按 pk 顺序锁 Invoice 批次后标记过期

2. **乐观并发（唯一约束 + 重试）**——用于分配当前支付指引的路径。
   不锁候选支付组合，依赖 uniq_invoice_active_payment
   部分唯一约束防冲突，外层 IntegrityError 重试循环处理并发碰撞。

   - Invoice.select_method() — 锁 Invoice，分配当前支付指引 → UPDATE → 重试

   原因：并发账单可能同时选择同一支付组合；唯一约束是最终一致性
   边界，IntegrityError 重试负责推进到下一个可用组合。

新增涉及 Invoice 支付指引并发写入的路径时，必须遵守上述协议。
"""
