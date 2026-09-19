# Mirror Memory v2

面向 AI 对话产品的长期记忆基础设施。

## 与 v1 的关系

Mirror Memory v1 是概念验证版本，验证了"AI 跨会话记忆"的可行性。v2 是从零开始的重写，保留了 v1 的核心理念，重新设计了架构：

| 维度 | v1 | v2 |
|---|---|---|
| 架构 | 单体，无事务保证 | 三阶段 Claim→Compute→Publish |
| 并发安全 | 无 | SELECT FOR UPDATE 行锁 + CAS |
| 删除保证 | 软删除 | 代际管理，备份恢复后删除重放 |
| 多租户 | 无 | tenant/app/subject 三级隔离 |
| 授权 | 无 | 版本化快照，purpose 绑定 |
| 提取器 | 无 | 规则 + LLM 辅助（DeepSeek） |
| 测试 | 无 | 116+ 测试，SQLite + PostgreSQL 双数据库 |

v1 不兼容，无迁移路径。

## 适用场景

Mirror Memory 专为**需要安全记忆管理的对话 AI** 设计，不是通用记忆框架。

### 适合

- **心理干预助手** — 用户说"我不想再提这件事"，必须真的忘记；纠正必须立刻生效
- **医疗健康 AI** — 病史记录不能编造，来源必须可溯
- **企业多租户** — A 公司数据绝不能出现在 B 公司的召回中
- **合规场景** — GDPR 被遗忘权，删除不可复活

### 不适合

- 追求最高召回率的通用聊天机器人（用 Mem0）
- 不需要跨会话记忆的单次对话
- 不需要删除/纠正能力的只读知识库

## 核心设计

三阶段 Worker，每个阶段独立事务：

```
Claim (T1)              Compute              Publish (T2)
CAS lease + auth check   Rule/LLM extraction  SELECT FOR UPDATE barrier
Load evidence snapshot   No DB access         Write atoms + CAS view
Commit → immutable       Safe to fail         Fencing complete
ticket                                        Commit or rollback
```

6 条不可破坏的不变量，15 个并发/阶段交错测试。

## 快速开始

```bash
pip install -e ".[dev]"

# SQLite（开发）
export DATABASE_URL="sqlite:///./mirror_memory.db"
export MIRROR_ENV="test"
python -m mirror_memory.cli init

# PostgreSQL（生产）
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/mirror_memory"
python -m mirror_memory.cli init

# 测试
pytest -v

# HTTP API
uvicorn mirror_memory.api:app --reload
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/auth/grant` | 授权 |
| POST | `/v1/auth/revoke` | 撤销 |
| POST | `/v1/observe` | 观察用户消息 |
| POST | `/v1/recall` | 召回记忆 |
| POST | `/v1/correct` | 纠正记忆 |
| POST | `/v1/forget` | 删除记忆 |
| POST | `/v1/explain` | 解释来源 |
| POST | `/v1/export` | 导出数据 |
| GET | `/v1/health` | 健康检查 |

## CLI

```bash
python -m mirror_memory.cli status
python -m mirror_memory.cli observe --tenant t1 --app psych --subject u1 --text "我喜欢猫"
python -m mirror_memory.cli recall --tenant t1 --app psych --subject u1 --query "宠物"
python -m mirror_memory.cli auth grant --tenant t1 --app psych --subject u1
```

## 测试结果

```
ruff:  0 errors
mypy:  0 errors
SQLite:     116 passed
PostgreSQL: 131 passed
```

## 文档

| 文档 | 说明 |
|---|---|
| [需求契约](docs/requirements.md) | R01-R15 验收标准 |
| [详细设计](docs/design.md) | 接口契约、数据模型、事务 |
| [不变量](docs/invariants.md) | 6 条不可破坏的不变量 |
| [执行指南](docs/execution-guide.md) | M0-M4 分阶段推进 |
| [验证指南](docs/validation.md) | C01-C20 契约测试、Q01-Q08 质量场景 |
| [预算设计](docs/budget.md) | R11 预算护栏 |
| [接入指南](docs/integration-guide.md) | 开发者接入流程 |
| [运行手册](docs/operations-playbook.md) | CLI 命令、故障处理 |
| [决策记录](docs/decisions/) | 架构决策 |

## 质量基线 (T09)

| 配置 | 提取率 | 期望命中 | 注入率 |
|---|---|---|---|
| 规则提取 | 58% | 83% | 8% |
| LLM 辅助 (DeepSeek) | 75% | 100% | 17% |

详细报告: `results/t09/T09_QUALITY_REPORT.md`

## 项目状态

- **M0-M1**: 完成 — 核心内核、12 张表、8 个仓库、三阶段 Worker
- **M2 (部分)**: 预算护栏、HTTP API、LLM 提取器、质量基线
- **M2 (待做)**: Psych 真实集成、模型质量基线
- **M3**: 内部验收（需要 Psych 代码 + 测试者）
- **M4**: 筆潤智談集成（需要第二产品代码）

## License

Apache 2.0