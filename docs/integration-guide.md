# Mirror Memory v2 — 接入指南

本指南面向首次接入 Mirror Memory 的开发者。按以下步骤即可完成"记住 → 等待 → 召回 → 纠正 → 解释 → 删除"完整流程，无需作者口头补充。

## 1. 环境准备

```bash
# 安装依赖
pip install -e ".[dev]"

# 设置环境变量
export DATABASE_URL="sqlite:///./mirror_memory.db"  # 开发环境
export MIRROR_ENV="test"                             # 测试环境

# 初始化数据库
python -m mirror_memory.cli init
```

生产环境使用 PostgreSQL：
```bash
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/mirror_memory"
export MIRROR_ENV="production"
```

## 2. 核心概念

- **Scope**: 租户 + 应用 + 用户的三元组，隔离所有数据
- **Authorization**: 版本化的授权快照，每个操作必须验证
- **Evidence**: 用户消息事件，是记忆的来源
- **Memory Atom**: 从证据中提取的结构化记忆单元
- **Job**: 处理任务，经历 pending → running → ready 生命周期

## 3. 六步示例

### Step 1: 授权用户

```python
from mirror_memory.application.memory_service import MirrorMemoryService
from mirror_memory.core.types import *

svc = MirrorMemoryService(session)
scope = Scope(tenant_id="my_tenant", app_id="my_app", subject_id="user_001")
ctx = MemoryContext(scope=scope, purpose="memory_management", session_id="session_1")

# 授权
auth = AuthorizationSnapshot(
    scope=scope, purpose="memory_management",
    allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
    version=1, issued_at=now, expires_at=now + timedelta(hours=1), issuer="my_backend",
)
svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=auth))
```

### Step 2: 观察用户消息

```python
result = svc.observe(ObserveInput(
    context=ctx,
    source_event=SourceEvent(
        source_event_id="msg_001",
        source_role=SourceRole.USER,
        text="技术问题请展开解释，我喜欢详细的回答",
        occurred_at=datetime.now(timezone.utc),
    ),
))
# result.success == True
# result.outcome.operation_id == "..." (job ID)
```

### Step 3: 等待处理完成

```python
# 处理任务（M1 使用确定性提取器）
from mirror_memory.runtime.worker import JobWorker
worker = JobWorker(session, scope, operation_id, "worker_1")
worker_result = worker.process()
# worker_result["status"] == "completed"
```

### Step 4: 召回记忆

```python
result = svc.recall(RecallInput(
    context=MemoryContext(scope=scope, purpose="memory_management", session_id="session_2"),
    query="技术回答偏好",
))
# result.outcome.outcome == RecallOutcome.FOUND
# result.outcome.items == [RecallItem(...)]
```

### Step 5: 纠正记忆

```python
result = svc.correct(CorrectInput(
    context=ctx,
    target_memory_id="...",
    expected_revision=1,
    correction_text="以后技术问题也先简短回答",
    user_correction_event_id="msg_002",
))
# result.outcome.old_blocked == True
```

### Step 6: 删除记忆

```python
result = svc.forget(ForgetInput(
    context=ctx,
    selector=ForgetSelector(memory_ids=["..."]),
    request_id="delete_001",
))
# result.outcome.status == "verified"
```

## 4. CLI 命令

```bash
# 查看状态
python -m mirror_memory.cli status

# 发送观察
python -m mirror_memory.cli observe \
  --tenant my_tenant --app my_app --subject user_001 \
  --text "我喜欢Python" --event-id cli_001

# 召回记忆
python -m mirror_memory.cli recall \
  --tenant my_tenant --app my_app --subject user_001 \
  --query "编程偏好"

# 纠正记忆
python -m mirror_memory.cli correct \
  --tenant my_tenant --app my_app --subject user_001 \
  --memory-id <id> --revision 1 --text "新偏好"

# 删除记忆
python -m mirror_memory.cli forget \
  --tenant my_tenant --app my_app --subject user_001 \
  --memory-ids "<id1>,<id2>"

# 验证删除
python -m mirror_memory.cli verify-deletion \
  --tenant my_tenant --app my_app --subject user_001

# 授权管理
python -m mirror_memory.cli auth grant \
  --tenant my_tenant --app my_app --subject user_001 --version 1

python -m mirror_memory.cli auth revoke \
  --tenant my_tenant --app my_app --subject user_001 --version 2

# 查看任务队列
python -m mirror_memory.cli jobs --scope-filter \
  --tenant my_tenant --app my_app --subject user_001
```

## 5. 产品适配器接入

```python
# 产品适配器示例
# from myapp.adapter import MyAdapter

adapter = MyAdapter(session, app_id="my_app")

# 设置模式
adapter.set_mode(AdapterMode.SHADOW)  # 或 ACTIVE_INTERNAL
adapter.set_test_cohort({"user_001", "user_002"})

# 授权用户
adapter.authorize_user("tenant_1", "user_001")

# 处理消息
result = adapter.observe_message("tenant_1", "user_001", "session_1", "我喜欢猫")

# 召回记忆
recall = adapter.recall_for_reply("tenant_1", "user_001", "session_1", "宠物偏好")
# recall["in_reply"] == True (仅 ACTIVE_INTERNAL + 在测试名单中)

# 格式化为 prompt 上下文
context_text = adapter.format_memories_for_context(recall["memories"])
```

## 6. 测试验证

```bash
# 运行全部测试
pytest -v

# 运行特定契约测试
pytest tests/contract/test_C01_isolation.py -v

# 运行质量场景
pytest tests/quality/ -v

# 运行集成测试
pytest tests/integration/ -v
```

## 7. 错误处理

所有操作返回统一的 `ResultEnvelope`：
- `success`: 是否成功
- `reason_code`: 错误码 (AUTH_DENIED, AUTH_EXPIRED, SCOPE_INVALID, etc.)
- `reason`: 人类可读的错误描述
- `retryable`: 是否可重试

常见错误及处理：
| 错误码 | 原因 | 处理 |
|---|---|---|
| AUTH_DENIED | 未授权或操作不在许可范围 | 调用 sync_authorization 授权 |
| AUTH_EXPIRED | 授权已过期 | 重新授权 |
| SCOPE_INVALID | 跨域访问 | 检查 scope 参数 |
| IDEMPOTENCY_CONFLICT | 重复事件内容不同 | 检查 source_event_id |
| REVISION_CONFLICT | 版本冲突 | 重新读取后重试 |

## 8. 命令登记表

| 命令 | 用途 | 环境 |
|---|---|---|
| `python -m mirror_memory.cli init` | 初始化数据库 | 所有 |
| `python -m mirror_memory.cli status` | 查看系统状态 | 所有 |
| `python -m mirror_memory.cli observe` | 发送观察 | 测试 |
| `python -m mirror_memory.cli recall` | 召回记忆 | 所有 |
| `python -m mirror_memory.cli correct` | 纠正记忆 | 所有 |
| `python -m mirror_memory.cli forget` | 删除记忆 | 所有 |
| `python -m mirror_memory.cli explain` | 解释来源 | 所有 |
| `python -m mirror_memory.cli verify-deletion` | 验证删除 | 所有 |
| `python -m mirror_memory.cli auth` | 管理授权 | 所有 |
| `python -m mirror_memory.cli jobs` | 查看队列 | 所有 |