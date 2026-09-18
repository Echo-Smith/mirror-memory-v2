"""Abstract memory service interface.

This defines the 8 operations that Mirror Memory v2 exposes.
Implementations must validate authorization before every protected operation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from mirror_memory.core.types import (
    CorrectInput,
    ExplainInput,
    ExportInput,
    ForgetInput,
    GetOperationInput,
    ObserveInput,
    RecallInput,
    ResultEnvelope,
    SyncAuthorizationInput,
)


class MemoryService(ABC):
    """Abstract interface for all Mirror Memory operations.

    Design contract (design.md §2):
    - Every protected operation validates allowed_operations, expiry, and version
    - Return envelope: request_id, outcome, reason_code, retryable, retry_after
    """

    @abstractmethod
    def observe(self, inp: ObserveInput) -> ResultEnvelope:
        """Accept a user event for processing.

        Returns accepted/rejected. Accepted means Evidence + job co-committed.
        Does NOT mean the memory has been formed yet.
        """
        ...

    @abstractmethod
    def get_operation(self, inp: GetOperationInput) -> ResultEnvelope:
        """Query processing job status.

        Must not read another subject's operation status.
        """
        ...

    @abstractmethod
    def recall(self, inp: RecallInput) -> ResultEnvelope:
        """Retrieve current or historical memories.

        Must return distinguishable outcomes: found/pending/no_memory/denied/timeout.
        Waiting past deadline must not return fake empty.
        """
        ...

    @abstractmethod
    def correct(self, inp: CorrectInput) -> ResultEnvelope:
        """Mark a specific memory target as corrected by the user.

        Stops old version from current recall after confirmation.
        New result may form asynchronously.
        """
        ...

    @abstractmethod
    def forget(self, inp: ForgetInput) -> ResultEnvelope:
        """Delete memories matching an explicit selector.

        Empty selector is rejected — not interpreted as 'delete all'.
        Deletion is verified before marking complete.
        """
        ...

    @abstractmethod
    def explain(self, inp: ExplainInput) -> ResultEnvelope:
        """Get source chain and reasoning behind a memory.

        Deleted memories must not leak content through history audit.
        """
        ...

    @abstractmethod
    def export(self, inp: ExportInput) -> ResultEnvelope:
        """Async export of user data.

        Authorization re-validated before delivery.
        Temporary files have expiry cleanup.
        """
        ...

    @abstractmethod
    def sync_authorization(self, inp: SyncAuthorizationInput) -> ResultEnvelope:
        """Sync a versioned authorization event from a trusted provider.

        Version regression is rejected. Same version + same content is idempotent.
        """
        ...
