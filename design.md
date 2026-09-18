# 详细设计与 M1 接口契约

状态：待实现的设计约定；本文件不提供现成 SDK。依据 [需求](requirements.md)，实现任务见 [tasks](tasks.md)。

## 1. 最小结构

目标仓库为 mirror-memory。计划在单一包内组织 core（类型/状态机）、application（用例/事务）、domains（规则与适配）、adapters（存储/模型/检索）、runtime（任务）。Psych 工作区目前仅暂存文档。

M1 使用确定性 extractor、真实 PostgreSQL、可注入时钟和假模型客户端。M2 增加一个实际提取 provider；M1 不调用付费模型、不要求向量扩展。每个环境使用独立数据库与凭据，测试启动时校验环境标记，不能对任意 DATABASE_URL 执行重置。

## 2. 调用上下文和返回值

`MemoryContext` 由可信业务后端绑定 tenant_id、app_id、subject_id、purpose；agent_id/session_id 可选，不能通过省略它们解除应用隔离。终端用户提交的 JSON 不能直接成为可信上下文。

`AuthorizationSnapshot` 至少包含 scope、purpose、allowed_operations、version、issued_at、expires_at、issuer。M1 为进程内可信 provider 契约，不提前实现通用 OAuth；HTTP 交付时另加调用方认证，序列化快照本身不构成认证。

| 操作（设计名称） | 关键输入 | 输出及语义 |
| --- | --- | --- |
| observe | context、source_event_id、source_role、text、occurred_at、session、retention_profile | operation_id、accepted/rejected；仅持久接收，不等于已形成 |
| get_operation | context、operation_id | pending/running/ready/failed/cancelled/expired、reason；不得读取他人状态 |
| recall | context、query 或 memory_type、mode、deadline、item/token budget、可选 after_operation_id | outcome、items、receipt_id、view_revision、freshness；等待超时不返回假 empty |
| correct | context、目标 ID 和预期修订、用户纠正事件 | 旧目标被阻断后的 receipt；修订冲突要求重读，不默认纠正另一个目标 |
| forget | context、显式 selector、request_id | deletion_receipt：blocked/purging/verified/failed；不接受空 selector=全部 |
| explain | context、memory_id 或 receipt_id | 当前获准查看的来源与理由；删除后不通过历史审计泄露内容 |
| export | context、明确范围、request_id | 异步导出状态；交付前再验权，临时文件有到期清理，排除令牌和内部秘密 |
| sync_authorization | 可信 provider 的版本化事件 | applied_version、ack；版本倒退拒绝，同版本同内容幂等 |

返回 envelope 统一 request_id、outcome、reason_code、retryable、可选 retry_after。可用错误码包括 AUTH_DENIED、AUTH_EXPIRED、SCOPE_INVALID、IDEMPOTENCY_CONFLICT、REVISION_CONFLICT、CAPACITY_LIMIT、PROVIDER_UNAVAILABLE、DEADLINE_EXCEEDED。不存在与无权限目标使用不暴露存在性的响应。HTTP 状态映射待 HTTP 阶段冻结。

`RecallItem` 包含 memory_id、revision、type、content、source_kind、valid_interval、evidence_refs、selection_reason。诊断信息按需返回；业务只需内容与标识，不必理解内部表结构。记忆仍为不可信数据，render 不拼接可执行指令。

## 3. 接收与处理的终态

```text
接收事务成功 → pending → running → ready
                          ├→ pending（可重试）
                          ├→ failed（重试耗尽/永久错误）
                          ├→ cancelled（撤销/删除）
                          └→ expired（来源保留到期）
```

ready 可表示“处理完成但没有可保存记忆”，使用 reason=no_memory；与未经处理的 pending 区分。延后或失败不丢失已接收 Evidence，但仍受到期删除约束。失败重放是显式管理动作，不能无限重试。

审核状态、生命周期、处理状态分开。M1 的“批准”可由受版本控制的确定性规则给出，不隐含必须人工逐条审核；M2 模型只能提交候选，校验与领域规则决定批准。无法判断的冲突保留候选，当前视图不发布武断结论。

## 4. 持久化与事务边界

逻辑集合：scope_control、evidence、jobs、generation_runs、atoms、relations、view_revisions、view_heads、lifecycle_events、receipts、budget_reservations、usage_ledger、deletion_jobs。可合并部分表，但不能牺牲下列约束。

| 约束 | 设计要求 |
| --- | --- |
| 引用隔离 | 外键/应用校验共同验证 tenant/app/subject；只验证 UUID 存在不够 |
| 接收幂等 | 唯一键 scope + source_event_id；对比规范化 payload 摘要，同键不同内容失败；摘要也按个人数据管理 |
| 处理幂等 | scope + evidence 集合指纹 + operation/domain/prompt version；独立证据计数由来源 ID 集合计算 |
| 当前视图 | head 使用 expected_revision 比较更新；失配读取新版本后重新整合 |
| 租约 | 每次领取产生递增 fencing token；提交须匹配当前 token，超时旧 worker 无权提交 |
| 删除/撤销 | scope_control 保存授权版本及删除 generation；候选提交核对开始时与当前版本 |
| 预算 | 短事务预留 cost/token/call 并发额度；结算幂等，未知结果保守对账 |

设计事务顺序统一先锁 scope_control，再锁作业/视图等对象，减少死锁；超时有界重试。observe 在同事务检查授权/删除屏障、写 Evidence 和任务。撤销、forget 阻断、结果提交使用相同控制记录协调，避免“检查后撤销、仍然提交”。模型网络请求在事务外执行。

发送模型前先验权并登记 dispatch；它定义外发许可点。此点之后的在途请求可能与撤销重叠，无法保证撤销瞬间网络上没有数据；应记录并尝试取消，返回后不得发布已撤销结果。回忆返回也有最后授权复核点，不能声称追回已经交给业务的内容。

系统不承诺外部模型调用恰好一次。旧 worker 即使结果被拒，费用可能已发生。模型请求超时与服务进程崩溃都要留下可对账的 run/reservation。

## 5. 授权握手和更新

1. 业务确认用户授权，provider 输出绑定用途和操作的有效快照；Mirror 建立/更新本地执行屏障。
2. 每次受保护操作验证允许项、有效期和本地最新版本；后台任务不能永久复用入队时快照。
3. 业务撤销先更新自己的授权源、停止新派发，并可靠重试 sync；Mirror 提交屏障后 ack。
4. ack 前的传播窗口单独测量；未知或过期授权拒绝新处理。provider 不可用时不以旧许可永久放行。
5. 新授权必须更高版本；删除后重新授权不会复活旧 generation 数据。

close_memory、delete_memory 和账户注销是不同动作。关闭停止使用/生成；删除清理指定范围；注销由业务协调所有系统。撤销记忆同意不能阻止业务经独立身份验证提交用户删除请求；forget/export 使用单独的管理权限。

## 6. 删除、历史与重新摄取

删除 selector 首版支持 subject 全量和明确 evidence/memory 集合，不支持随意执行查询表达式。删除一个 atom 时禁止同一来源任务立即重生它；按依赖链阻断相关视图并清理受影响来源，若扩大删除范围需在 selector 预览中明确。

保留一个最小禁止重放记录直到所有相关任务、备份与导入窗口失效。它不包含原文；定位标识需受控。来源删除时相关派生记忆整体失效，待仍存活的独立来源重新形成，首版不尝试从混合摘要中猜测删除部分。

相同 source_event_id 的历史重放被拒绝。用户日后重新表达相同偏好且当前授权有效，可作为新事件记住；系统无法仅靠语义保证“永不再记起这个主题”。业务不可把旧历史换新 ID 重灌绕过删除；历史导入不在首版范围。

当前查询排除 superseded/dormant；历史查询可以返回仍在保留期内的历史断言，但不能返回已删除、越权或推断为当前有效的内容。expires_at 执行保留，valid_until 描述事实适用性，两者不可互换。

## 7. Psych 与下一产品

Psych adapter 将允许的消息和业务授权转成通用输入，提供偏好/事件规则以及输出呈现；即时危机流程继续由 Psych 负责。Mirror 的 schema 不出现危机等级、量表分数等必填字段。

内部环境支持 off / shadow / active_internal 三种接入模式（拟实现）：shadow 不进入实际回复，active_internal 仅测试名单与独立会话可见。记录模式与版本；回退为 off 不等于删除已采集数据，删除单独执行。

笔润智谈接入使用新 app_id、授权和数据集；评测语言、格式、任务偏好是否只需新增领域规则。修改核心时同步回归所有旧契约，不能用条件分支识别产品名来绕过统一规则。
