# Mirror Memory v2

Long-term memory infrastructure for AI conversational products.

## What It Does

Mirror Memory lets AI assistants remember users across sessions — preferences, plans, facts — while giving users full control to correct, close, and delete those memories.

**Not a chat app.** It's an infrastructure layer (SDK + backend) that products like Psych and 筆潤智談 integrate with.

## Architecture

Three-phase Worker: **Claim → Compute → Publish**

```
Claim (T1)              Compute              Publish (T2)
CAS lease + auth check   Rule extraction      SELECT FOR UPDATE barrier
Load evidence snapshot   No DB access         Write atoms + CAS view
Commit → immutable       Safe to fail         Fencing complete
ticket                                        Commit or rollback
```

6 invariants (I1–I6) with concurrent/phase-interleaving tests on PostgreSQL.

## Quick Start

```bash
pip install -e ".[dev]"

# SQLite (development)
export DATABASE_URL="sqlite:///./mirror_memory.db"
export MIRROR_ENV="test"
python -m mirror_memory.cli init

# PostgreSQL (production)
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/mirror_memory"
python -m mirror_memory.cli init

# Run tests
pytest -v

# Start HTTP API
uvicorn mirror_memory.api:app --reload
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/v1/auth/grant` | Grant authorization |
| POST | `/v1/auth/revoke` | Revoke authorization |
| POST | `/v1/observe` | Accept user event |
| POST | `/v1/recall` | Recall memories |
| POST | `/v1/correct` | Correct a memory |
| POST | `/v1/forget` | Delete memories |
| POST | `/v1/explain` | Explain source chain |
| POST | `/v1/export` | Export user data |
| POST | `/v1/operation` | Query operation status |
| GET | `/v1/health` | Health check |

## CLI Commands

```bash
python -m mirror_memory.cli status          # System status
python -m mirror_memory.cli observe --help  # Send observation
python -m mirror_memory.cli recall --help   # Recall memories
python -m mirror_memory.cli correct --help  # Correct memory
python -m mirror_memory.cli forget --help   # Delete memories
python -m mirror_memory.cli explain --help  # Explain source
python -m mirror_memory.cli verify-deletion --help
python -m mirror_memory.cli auth grant --help
python -m mirror_memory.cli jobs --help
```

## Documentation

| Document | Purpose |
|---|---|
| [Requirements](docs/requirements.md) | R01–R15 acceptance criteria |
| [Design](docs/design.md) | Interface contracts, data model, transactions |
| [Tasks](docs/tasks.md) | T00–T14 implementation tracking |
| [Execution Guide](docs/execution-guide.md) | M0–M4 phased approach |
| [Validation](docs/validation.md) | C01–C20 contract tests, Q01–Q08 quality scenarios |
| [Operations](docs/operations.md) | Runbooks, incident response |
| [Invariants](docs/invariants.md) | 6 non-breakable invariants (I1–I6) |
| [Budget](docs/budget.md) | Budget guardrail design (R11) |
| [Integration Guide](docs/integration-guide.md) | Developer onboarding walkthrough |
| [Operations Playbook](docs/operations-playbook.md) | CLI commands, failure handling |
| [Decisions](docs/decisions/) | Architecture decision records |

## Test Results

```
ruff:  0 errors
mypy:  0 errors
SQLite:     139 passed, 16 skipped
PostgreSQL: 154 passed, 1 skipped
```

## Project Status

- **M0–M1**: Complete — core kernel, 12 tables, 8 repositories, 3-phase worker
- **M2 (partial)**: Psych adapter, budget guardrails, HTTP API, deterministic extractor
- **M2 (pending)**: Real Psych integration, model quality baseline
- **M3**: Internal acceptance (requires Psych code + test users)
- **M4**: 筆潤智談 integration (requires second product code)