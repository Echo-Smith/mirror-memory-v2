"""Mirror Memory CLI — management commands for operations.

Commands:
  init      — Initialize database schema
  status    — Show system status (DB connection, scope count, job stats)
  observe   — Send a test observation
  recall    — Recall memories for a scope
  correct   — Correct a memory atom
  forget    — Delete memories for a scope
  explain   — Explain a memory's source chain
  export    — Export user data
  auth      — Manage authorization (grant/revoke/list)
  jobs      — Show job queue status
  verify-deletion — Verify deletion completeness

Usage:
  python -m mirror_memory.cli <command> [options]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mirror_memory.adapters.postgresql.models import (
    Base,
    Evidence,
    Job,
    MemoryAtomModel,
    ScopeControl,
)
from mirror_memory.adapters.postgresql.repositories import (
    MemoryAtomRepository,
    ScopeControlRepository,
)
from mirror_memory.application.memory_service import MirrorMemoryService
from mirror_memory.core.types import (
    AuthorizationSnapshot,
    CorrectInput,
    ExplainInput,
    ForgetInput,
    ForgetSelector,
    MemoryContext,
    ObserveInput,
    RecallInput,
    Scope,
    SourceEvent,
    SourceRole,
    SyncAuthorizationInput,
)


def _get_session() -> Session:
    url = os.environ.get("DATABASE_URL", "sqlite:///./mirror_memory.db")
    engine = create_engine(url)
    return sessionmaker(bind=engine)()


def _scope(tenant: str, app: str, subject: str) -> Scope:
    return Scope(tenant_id=tenant, app_id=app, subject_id=subject)


def cmd_init(args):
    """Initialize database schema."""
    url = os.environ.get("DATABASE_URL", "sqlite:///./mirror_memory.db")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    print(f"Schema created at {url}")
    print(f"Tables: {len(Base.metadata.tables)}")


def cmd_status(args):
    """Show system status."""
    session = _get_session()
    try:
        scope_count = session.query(ScopeControl).count()
        evidence_count = session.query(Evidence).count()
        job_count = session.query(Job).count()
        atom_count = session.query(MemoryAtomModel).count()

        pending = session.query(Job).filter(Job.state == "pending").count()
        failed = session.query(Job).filter(Job.state == "failed").count()

        print("=== Mirror Memory Status ===")
        print(f"Scopes:    {scope_count}")
        print(f"Evidence:  {evidence_count}")
        print(f"Atoms:     {atom_count}")
        print(f"Jobs:      {job_count} (pending: {pending}, failed: {failed})")
    finally:
        session.close()


def cmd_observe(args):
    """Send a test observation."""
    session = _get_session()
    try:
        svc = MirrorMemoryService(session)
        scope = _scope(args.tenant, args.app, args.subject)
        ctx = MemoryContext(scope=scope, purpose=args.purpose or "memory_management",
                           session_id=args.session)

        # Authorize first
        now = datetime.now(UTC)
        auth = AuthorizationSnapshot(
            scope=scope, purpose="memory_management",
            allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
            version=1, issued_at=now, expires_at=now + timedelta(hours=1), issuer="cli",
        )
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=auth))

        result = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id=args.event_id,
                source_role=SourceRole(args.role),
                text=args.text,
                occurred_at=now,
                session_id=args.session,
            ),
        ))

        if result.success:
            session.commit()
            print(f"✓ Accepted: operation_id={result.outcome.operation_id}")
        else:
            session.rollback()
            print(f"✗ Rejected: {result.reason} ({result.reason_code})")
            sys.exit(1)
    finally:
        session.close()


def cmd_recall(args):
    """Recall memories for a scope."""
    session = _get_session()
    try:
        svc = MirrorMemoryService(session)
        scope = _scope(args.tenant, args.app, args.subject)
        ctx = MemoryContext(scope=scope, purpose="memory_management", session_id=args.session)

        result = svc.recall(RecallInput(
            context=ctx, query=args.query, mode=args.mode,
        ))

        if result.success:
            outcome = result.outcome
            print(f"Outcome: {outcome.outcome.value}")
            if outcome.items:
                for i, item in enumerate(outcome.items):
                    print(f"  [{i+1}] ({item.type.value}) {item.content}")
                    print(f"      source={item.source_kind.value} id={item.memory_id}")
            if outcome.receipt_id:
                print(f"Receipt: {outcome.receipt_id}")
        else:
            print(f"✗ Failed: {result.reason}")
            sys.exit(1)
    finally:
        session.close()


def cmd_correct(args):
    """Correct a memory atom."""
    session = _get_session()
    try:
        svc = MirrorMemoryService(session)
        scope = _scope(args.tenant, args.app, args.subject)
        ctx = MemoryContext(scope=scope, purpose="memory_management")

        result = svc.correct(CorrectInput(
            context=ctx, target_memory_id=args.memory_id,
            expected_revision=args.revision,
            correction_text=args.text,
            user_correction_event_id=args.event_id or f"cli_correct_{args.memory_id}",
        ))

        if result.success:
            session.commit()
            print(f"✓ Corrected: old_blocked={result.outcome.old_blocked}")
        else:
            session.rollback()
            print(f"✗ Failed: {result.reason}")
            sys.exit(1)
    finally:
        session.close()


def cmd_forget(args):
    """Delete memories for a scope."""
    session = _get_session()
    try:
        svc = MirrorMemoryService(session)
        scope = _scope(args.tenant, args.app, args.subject)
        ctx = MemoryContext(scope=scope, purpose="memory_management")

        selector = ForgetSelector(
            memory_ids=args.memory_ids.split(",") if args.memory_ids else None,
            scope_wide=args.scope_wide,
        )

        result = svc.forget(ForgetInput(
            context=ctx, selector=selector,
            request_id=args.request_id or f"cli_forget_{datetime.now(UTC).timestamp()}",
        ))

        if result.success:
            session.commit()
            print(f"✓ Deleted: status={result.outcome.status}")
        else:
            session.rollback()
            print(f"✗ Failed: {result.reason}")
            sys.exit(1)
    finally:
        session.close()


def cmd_explain(args):
    """Explain a memory's source chain."""
    session = _get_session()
    try:
        svc = MirrorMemoryService(session)
        scope = _scope(args.tenant, args.app, args.subject)
        ctx = MemoryContext(scope=scope, purpose="memory_management")

        result = svc.explain(ExplainInput(context=ctx, memory_id=args.memory_id))

        if result.success:
            out = result.outcome
            print(f"Memory: {out.memory_id}")
            print(f"Revisions: {out.revision_history}")
            if out.source_chain:
                print("Source chain:")
                for src in out.source_chain:
                    print(f"  - {src}")
        else:
            print(f"✗ Failed: {result.reason}")
            sys.exit(1)
    finally:
        session.close()


def cmd_verify_deletion(args):
    """Verify deletion completeness."""
    session = _get_session()
    try:
        scope = _scope(args.tenant, args.app, args.subject)
        sc_repo = ScopeControlRepository(session)
        atom_repo = MemoryAtomRepository(session)

        sc = sc_repo.get(scope)
        if sc is None:
            print(f"Scope {scope.key()} not found")
            return

        if sc.deleted_at:
            print(f"Scope deleted at: {sc.deleted_at}")
            print(f"Deletion generation: {sc.deletion_generation}")
        else:
            print("Scope not deleted")

        current = atom_repo.get_current(scope)
        print(f"Current atoms: {len(current)}")
        if current:
            print("WARNING: atoms still marked as current after deletion!")
            sys.exit(1)
        else:
            print("✓ Deletion verified: no current atoms remain")
    finally:
        session.close()


def cmd_auth(args):
    """Manage authorization."""
    session = _get_session()
    try:
        svc = MirrorMemoryService(session)
        scope = _scope(args.tenant, args.app, args.subject)
        now = datetime.now(UTC)

        if args.auth_action == "grant":
            auth = AuthorizationSnapshot(
                scope=scope, purpose="memory_management",
                allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
                version=args.version, issued_at=now,
                expires_at=now + timedelta(hours=args.hours), issuer="cli",
            )
            result = svc.sync_authorization(
                SyncAuthorizationInput(event_type="grant", snapshot=auth)
            )
            if result.success:
                session.commit()
            print(f"{'✓' if result.success else '✗'} {result.reason or f'version={args.version}'}")

        elif args.auth_action == "revoke":
            auth = AuthorizationSnapshot(
                scope=scope, purpose="memory_management",
                allowed_operations=[], version=args.version,
                issued_at=now, expires_at=now + timedelta(hours=1), issuer="cli",
            )
            result = svc.sync_authorization(
                SyncAuthorizationInput(event_type="revoke", snapshot=auth)
            )
            if result.success:
                session.commit()
            print(f"{'✓' if result.success else '✗'} {result.reason or 'revoked'}")

        elif args.auth_action == "list":
            sc_repo = ScopeControlRepository(session)
            sc = sc_repo.get(scope)
            if sc and sc.auth_version > 0:
                print(f"Version: {sc.auth_version}")
                print(f"Operations: {sc.auth_allowed_operations}")
                print(f"Expires: {sc.auth_expires_at}")
            else:
                print("No authorization")
    finally:
        session.close()


def cmd_jobs(args):
    """Show job queue status."""
    session = _get_session()
    try:
        if args.scope_filter:
            scope = _scope(args.tenant, args.app, args.subject)
            sc_repo = ScopeControlRepository(session)
            sc = sc_repo.get(scope)
            if sc is None:
                print("Scope not found")
                return
            jobs = session.execute(
                select(Job).where(Job.scope_id == sc.id).order_by(Job.created_at.desc()).limit(20)
            ).scalars().all()
        else:
            jobs = session.execute(
                select(Job).order_by(Job.created_at.desc()).limit(20)
            ).scalars().all()

        print(f"{'ID':<12} {'State':<10} {'Reason':<20} {'Retries':<8} {'Created'}")
        print("-" * 70)
        for j in jobs:
            print(f"{j.id[:10]:<12} {j.state:<10} {(j.reason or ''):<20} {j.retry_count:<8} {j.created_at}")
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="Mirror Memory CLI")
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Initialize database schema")

    # status
    sub.add_parser("status", help="Show system status")

    # observe
    p = sub.add_parser("observe", help="Send observation")
    p.add_argument("--tenant", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--event-id", default=None)
    p.add_argument("--role", default="user")
    p.add_argument("--session", default=None)
    p.add_argument("--purpose", default=None)

    # recall
    p = sub.add_parser("recall", help="Recall memories")
    p.add_argument("--tenant", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--query", default="")
    p.add_argument("--mode", default="current")
    p.add_argument("--session", default=None)

    # correct
    p = sub.add_parser("correct", help="Correct a memory")
    p.add_argument("--tenant", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--memory-id", required=True)
    p.add_argument("--revision", required=True, type=int)
    p.add_argument("--text", required=True)
    p.add_argument("--event-id", default=None)

    # forget
    p = sub.add_parser("forget", help="Delete memories")
    p.add_argument("--tenant", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--memory-ids", default=None)
    p.add_argument("--scope-wide", action="store_true")
    p.add_argument("--request-id", default=None)

    # explain
    p = sub.add_parser("explain", help="Explain memory source")
    p.add_argument("--tenant", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--memory-id", required=True)

    # verify-deletion
    p = sub.add_parser("verify-deletion", help="Verify deletion completeness")
    p.add_argument("--tenant", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--subject", required=True)

    # auth
    p = sub.add_parser("auth", help="Manage authorization")
    p.add_argument("auth_action", choices=["grant", "revoke", "list"])
    p.add_argument("--tenant", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--version", type=int, default=1)
    p.add_argument("--hours", type=float, default=24)

    # jobs
    p = sub.add_parser("jobs", help="Show job queue")
    p.add_argument("--tenant", default=None)
    p.add_argument("--app", default=None)
    p.add_argument("--subject", default=None)
    p.add_argument("--scope-filter", action="store_true")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "init": cmd_init, "status": cmd_status, "observe": cmd_observe,
        "recall": cmd_recall, "correct": cmd_correct, "forget": cmd_forget,
        "explain": cmd_explain, "verify-deletion": cmd_verify_deletion,
        "auth": cmd_auth, "jobs": cmd_jobs,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()