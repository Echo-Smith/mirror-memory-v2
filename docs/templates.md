# 执行记录模板

复制所需部分建立实际记录，不直接把本文件里的空值当运行配置。建议在目标仓库保存无敏感内容的记录；真实数据、凭据、Prompt 原文与个人明细放受控位置，只在报告引用不透明证据 ID。

## A. 阶段/批次卡

```text
record_id:
stage: M0 / M1 / M2 / M3 / M4
status: not_started / running / blocked / passed
owner_implementation:
owner_business:
owner_operations:
reviewer:
environment_id:
code_commit:
schema_version:
domain_prompt_model_versions:
profile_version:
dataset_version_and_split:
data_class: synthetic / authorized_internal
mode: offline / shadow / active_internal
batch_start:
batch_end:
authorization_provider_and_version:
allowed_processing_destinations:
retention_profile_and_expiry:
experiment_budget_currency_and_total: # 付费调用前必填
budget_stop_action:
quality_and_latency_thresholds: # 运行前冻结
entry_evidence:
task_and_test_ids:
results_links:
known_limits:
exit_decision_and_reason:
next_task:
```

填值规则：offline 合成规则测试不需要模型目的地；未填金额时禁止付费调用，不能将空值解释为无限；真实数据必须有有效授权、具体到期日和处理目的地；没有实测值的质量/容量字段写 pending 并说明阻塞哪个阶段。

## B. 授权握手与撤销样例记录

```text
case_id:
trusted_caller_id:
scope: tenant / app / opaque_subject
purpose:
operations:
version_before:
version_after:
issued_at / expires_at:
business_changed_at:
mirror_ack_at:
dispatch_or_commit_at:
expected_outcome:
actual_outcome:
propagation_delay:
receipt_id:
```

这里记录协议证据，不复制用户同意原文或密钥。version 由可信业务管理；测试不能用模型输出伪造该字段。

## C. 用量 profile 评审表

| 字段 | 填写依据 |
| --- | --- |
| profile_id / version / effective_at | 唯一版本与启用时间 |
| tested_environment / model / price_version | 测试环境与价格来源日期 |
| quality_floor / latency_limit | 业务认可的底线，必须先于候选评分 |
| input_size / max_output_tokens | 输入和单次调用边界 |
| app_window / subject_window | 时间窗口、公平性范围 |
| soft_limit / hard_limit | 费用、调用/token、并发、队列门槛分别填写，不混一个数字 |
| recovery_threshold / cooldown | 低于进入门槛的恢复条件和观察间隔 |
| total_experiment_budget | 运行方给定的损失上限，不能由模型自动提高 |
| measured_cost_per_1000_events | 包含重试，说明估算或实际账单 |
| ready_delay / recall_p95 / false_limit_rate | 实测值与样本量 |
| quality_counts_by_category | 原始分子/分母与总数 |
| why_this_operating_point | 选取当前工作点和放弃其他配置的原因 |
| rollback_profile | 上一个经过验证的版本 |
| decision / owner | 采纳、拒绝或继续实验 |

## D. 测试结果表

| ID | commit/config | 输入样例 ID | expected | actual | 状态 | 证据/问题 |
| --- | --- | --- | --- | --- | --- | --- |
| Cxx/Qxx | 待填 | 待填 | 待填 | 未执行 | not_run | 待填 |

状态仅取 not_run / passed / failed / not_applicable。对 N/A 写原因；列出失败不能只报通过率。关键失败未修复时放行结论为不通过。

## E. 发布/回退卡

```text
release_id:
scope_and_allowlist:
before_versions:
after_versions:
contract_report:
holdout_report:
cost_profile:
schema_forward_and_backward_compatibility:
backup_and_deletion_ledger_location_refs:
rollback_steps_verified_in_environment:
observation_window_and_stop_conditions:
operator_and_contact:
decision: internal_only / approved_scope / rejected
post_release_results:
```

回退步骤必须说明代码、schema、已形成记忆各如何处理，不能只写“回滚版本”。涉及实际生产或对外发布时需有该范围的明确授权。

## F. 巡检/事件/清理卡

```text
record_id / timestamp / environment:
authorization_lag:
overdue_deletions:
oldest_pending_job / expired_leases:
failure_categories:
settled_cost / reserved_cost / remaining_budget:
incident_or_deletion_id:
affected_scope_and_versions:
containment_action:
root_cause_or_current_hypothesis:
storage_cleanup_status: primary / projection / export / logs / backup / external
recovery_tests:
verification_time:
remaining_limits_and_owner:
next_check_at:
```

外部存储无法验证时填 unknown，不填 verified；备份等待窗口应有下次核验时间。

## G. 实际命令登记表（T03/T11 必须补齐）

本表当前没有可执行命令。实现者应填写完整命令或 SDK 管理入口、工作目录、必需环境、权限、输出与失败含义，并在合成环境执行验证。任何修改/删除命令必须明确环境与目标，支持预览或范围确认，不使用空 selector。

| 动作 | 预期结果 | 实际入口/命令 | 验证版本 |
| --- | --- | --- | --- |
| 初始化测试数据库与 migration | 目标环境 schema ready | 待 T01 实现 | 未验证 |
| 启动/停止 worker | heartbeat 可见；有界停止 | 待 T03/T04 实现 | 未验证 |
| 六步 SDK 示例 | observe→recall→correct→explain→forget 通过（含等待） | 待 T05/T06 实现 | 未验证 |
| 跑契约与质量回归 | 按编号导出结果，无原文泄露 | 待 T07/T09 实现 | 未验证 |
| 查看任务/授权/预算 | 只读状态与固定错误码 | 待 T11 实现 | 未验证 |
| 暂停/恢复模型任务 | 不阻断撤销和删除 | 待 T10/T11 实现 | 未验证 |
| 按 ID 重放失败任务 | 幂等且重新检查授权 | 待 T11 实现 | 未验证 |
| 删除范围预览/提交/验证 | 屏障与各副本状态 | 待 T06/T11 实现 | 未验证 |
| 导出并清理产物 | 有效权限与到期清理 | 待 T06 实现 | 未验证 |
| 重建投影 | 不调用生成模型、不复活删除项 | 待 T07/T11 实现 | 未验证 |
| 隔离备份恢复与删除重放 | 检查通过前无业务入口 | 待 T07/T13 实现 | 未验证 |

## H. 决策变更卡

```text
decision_id / date:
problem_with_current_behavior:
proposed_change:
affected_R_C_Q_ids:
alternatives_and_reason:
evidence:
impact_on_products:
rollback_or_exit:
status: proposed / accepted / rejected / superseded
owner:
```

需创建变更卡：新存储/队列依赖、公共接口破坏性变化、授权/保留语义变化、新领域要求修改内核、embedding 或自适应半衰期启用。普通实现细节修复无需增加一份决策文档。
