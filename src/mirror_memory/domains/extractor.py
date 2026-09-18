"""Deterministic rule-based extractor for M1.

No paid model calls. Extracts memory atoms from user text using pattern matching.
M2 adds a real extraction provider; this is the baseline.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar


class ExtractionResult:
    """Result of a deterministic extraction attempt."""

    def __init__(
        self,
        atoms: list[dict[str, Any]],
        confidence: float = 1.0,
        source_valid: bool = True,
        rejection_reason: str | None = None,
    ) -> None:
        self.atoms = atoms
        self.confidence = confidence
        self.source_valid = source_valid
        self.rejection_reason = rejection_reason


class DeterministicExtractor:
    """Rule-based extractor for user preferences, facts, and plans.

    Design constraints (R04):
    - assistant-role sources cannot become user facts
    - No source → no extraction
    - Injection patterns are rejected
    """

    # Patterns for preference extraction
    _preference_patterns: ClassVar[list[tuple[str, str]]] = [
        (r"(?:我|我喜欢|我偏好|请)(.*?)(?:的回答|解释|回答)", "preference"),
        (r"(?:技术问题|编程问题|代码问题)(?:请|要|希望)(.*?)(?:展开|详细|简短)", "preference"),
        (r"(?:以后|从现在起|今后)(.*?)(?:先|也先|都先)(.*?)回答", "preference"),
    ]

    # Patterns for plan extraction
    _plan_patterns: ClassVar[list[tuple[str, str]]] = [
        (r"(?:准备|打算|计划|六月|七月|八月)(.*?)(?:考试|面试|出差|旅行)", "plan"),
        (r"(?:已取消|取消了|不考了)(.*?)(?:考试|计划|安排)", "plan"),
    ]

    # Patterns for fact extraction
    _fact_patterns: ClassVar[list[tuple[str, str]]] = [
        (r"(?:我在|我在)(.*?)(?:公司|上班|工作)", "fact"),
        (r"(?:朋友|同事|同学)(.*?)(?:换了|辞了|去了)(.*?)(?:工作|公司)", "fact"),
    ]

    # Injection patterns to reject
    _injection_patterns: ClassVar[list[str]] = [
        r"忽略.*?授权",
        r"记住.*?其他.*?用户",
        r"ignore.*?auth",
        r"record.*?another.*?user",
        r"<script",
        r"javascript:",
        r"忘记.*?删除",
        r"delete.*?all.*?memory",
    ]

    def extract(
        self,
        text: str,
        source_role: str = "user",
        context: dict | None = None,
    ) -> ExtractionResult:
        """Extract memory atoms from text.

        Args:
            text: The source text to extract from.
            source_role: Who produced this text (user/assistant/system).
            context: Optional context (session info, etc.)

        Returns:
            ExtractionResult with atoms and metadata.
        """
        # Reject assistant speculation
        if source_role == "assistant":
            return ExtractionResult(
                atoms=[],
                source_valid=False,
                rejection_reason="assistant_speculation_not_user_fact",
            )

        # Reject system sources for memory extraction
        if source_role == "system":
            return ExtractionResult(atoms=[], confidence=0.0)

        # Reject injection attempts
        text_lower = text.lower()
        for pattern in self._injection_patterns:
            if re.search(pattern, text_lower):
                return ExtractionResult(
                    atoms=[],
                    source_valid=False,
                    rejection_reason="injection_detected",
                )

        # Extract atoms
        atoms = []

        # Check preferences
        for pattern, atom_type in self._preference_patterns:
            match = re.search(pattern, text)
            if match:
                content = match.group(0).strip()
                if content:
                    atoms.append({
                        "type": atom_type,
                        "content": content,
                        "confidence": 0.8,
                    })

        # Check plans
        for pattern, atom_type in self._plan_patterns:
            match = re.search(pattern, text)
            if match:
                content = match.group(0).strip()
                if content:
                    atoms.append({
                        "type": atom_type,
                        "content": content,
                        "confidence": 0.7,
                    })

        # Check facts
        for pattern, atom_type in self._fact_patterns:
            match = re.search(pattern, text)
            if match:
                content = match.group(0).strip()
                if content:
                    atoms.append({
                        "type": atom_type,
                        "content": content,
                        "confidence": 0.6,
                    })

        # If no patterns matched but text looks like a preference statement
        if not atoms and any(kw in text for kw in ["喜欢", "偏好", "prefer", "like"]):
            atoms.append({
                "type": "preference",
                "content": text.strip(),
                "confidence": 0.5,
            })

        return ExtractionResult(
            atoms=atoms,
            confidence=max(float(str(a["confidence"])) for a in atoms) if atoms else 0.0,
        )
