# Mirror Memory v2 — 运行手册

## O01: 跨用户泄露或授权失败

**症状**: 用户A看到用户B的记忆，或授权操作被拒绝
**排查**:
1. 检查 scope_control 表的 tenant_id/app_id/subject_id 是否正确
2. 验证 AuthorizationSnapshot.version 和 expires_at
3. 查看 lifecycle_events 表的 authorize/revoke 记录
**处置**: 立即停止受影响 scope 的服务，记录诊断证据，修复后重跑 C01/C02

## O02: Worker 卡死或队列积压

**症状**: Job 长时间处于 pending/running 状态
**排查**:
1. `python -m mirror_memory.cli jobs --scope-filter --tenant X --app Y --subject Z`
2. 检查 lease_expires_at 是否过期
3. 查看 worker 日志
**处置**:
- 未过期: 等待 lease 自动过期
- 已过期: 手动将 state 重置为 pending
- 反复卡死: 检查提取器是否抛出未捕获异常

## O03: 成本异常或护栏误触发

**症状**: 正常请求被拒绝，或成本超出预期
**排查**:
1. 查询 budget_reservations 和 usage_ledger 表
2. 检查是否有大量 failed_call 记录
3. 比对实际消耗与预算阈值
**处置**:
- 误限: 调整 profile 阈值，记录理由
- 真实超限: 检查是否有异常重试循环

## O04: 记忆错误或陈旧内容

**症状**: 召回的记忆不准确或已过时
**排查**:
1. 使用 `explain` 命令追溯来源
2. 检查 evidence 表的原始文本
3. 验证 memory_atoms 的 is_current 和 superseded_by
**处置**:
- 来源正确但提取错误: 修正提取器规则
- 来源本身错误: 使用 `correct` 命令修正
- 已过时: 使用 `forget` 命令删除

## O05: 删除超时或数据复活

**症状**: 删除请求未完成，或已删除数据重新出现
**排查**:
1. 检查 deletion_jobs 表的状态
2. 验证 scope_control.deletion_generation
3. 检查 memory_atoms.deletion_generation
**处置**:
- 删除未完成: 重新执行 forget 操作
- 数据复活: 检查是否有旧备份恢复，重放删除记录
- 紧急: 停止所有写入，手动清理后重启

## O06: 数据库不可用

**症状**: 所有操作返回 PROVIDER_UNAVAILABLE
**排查**:
1. 检查数据库连接: `python -m mirror_memory.cli status`
2. 验证 DATABASE_URL 环境变量
3. 检查数据库进程状态
**处置**:
- 不返回 accepted — 客户端应重试
- 已 accepted 的 Evidence 不丢失（事务保证）
- 恢复后检查 pending jobs 并重新处理

## 发布清单

- [ ] schema 版本兼容性检查
- [ ] 所有 C01-C15 契约测试通过
- [ ] 环境变量配置正确（DATABASE_URL, MIRROR_ENV）
- [ ] 授权提供者连通性验证
- [ ] 备份策略确认（7天滚动窗口）
- [ ] 告警联系人已登记
- [ ] 停止条件已明确定义

## 回退流程

1. 停止新请求接入
2. 回退代码到上一个已知良好版本
3. 验证 schema 兼容性
4. 检查是否有未完成的 deletion jobs
5. 恢复服务
6. 运行回归测试

## 命令登记表

| 命令 | 用途 | 危险级别 |
|---|---|---|
| `cli init` | 初始化数据库 | 低（幂等） |
| `cli status` | 查看状态 | 只读 |
| `cli observe` | 发送观察 | 低 |
| `cli recall` | 召回记忆 | 只读 |
| `cli correct` | 纠正记忆 | 中 |
| `cli forget` | 删除记忆 | **高** |
| `cli explain` | 解释来源 | 只读 |
| `cli verify-deletion` | 验证删除 | 只读 |
| `cli auth grant` | 授权 | 中 |
| `cli auth revoke` | 撤销授权 | **高** |
| `cli jobs` | 查看队列 | 只读 |