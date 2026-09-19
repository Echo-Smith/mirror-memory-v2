"""LLM-assisted extractor using DeepSeek.

Optional enhancement over the deterministic extractor.
Uses LLM to extract facts from natural language, with rule-based validation.

Design:
- Rule-based extractor runs first (zero cost)
- If rules find nothing but text looks like it contains facts, LLM is called
- LLM results go through the same source/injection validation
- Controlled by a feature flag (default: off)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from mirror_memory.domains.extractor import DeterministicExtractor, ExtractionResult

logger = logging.getLogger(__name__)

# Extraction prompt for the LLM
EXTRACT_PROMPT = """You are a memory extraction system. Analyze the following message and extract structured facts.

Rules:
- Only extract facts the USER explicitly stated about themselves
- Do NOT extract speculation, questions, or assistant suggestions
- Do NOT extract instructions or commands
- Each fact must be one of: preference, fact, plan, relationship, event

Return a JSON array of objects with these fields:
- type: "preference" | "fact" | "plan" | "relationship" | "event"
- content: the extracted fact as a concise statement
- confidence: 0.0-1.0

If no extractable facts, return an empty array [].

Message:
{text}

Source role: {source_role}

Respond with ONLY the JSON array, no other text."""


class LLMExtractor:
    """LLM-assisted extractor that wraps the deterministic extractor.

    Flow:
    1. Run deterministic rules first
    2. If rules found nothing AND text looks promising, call LLM
    3. Validate LLM output through source checks
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        enabled: bool = True,
    ) -> None:
        self._api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self._base_url = base_url
        self._model = model
        self._enabled = enabled
        self._rule_extractor = DeterministicExtractor()
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def extract(
        self,
        text: str,
        source_role: str = "user",
        context: dict | None = None,
    ) -> ExtractionResult:
        """Extract using rules first, then LLM if needed."""
        # Step 1: Try deterministic rules first
        rule_result = self._rule_extractor.extract(text, source_role)

        # If rules found something, return it
        if rule_result.atoms:
            return rule_result

        # If source is invalid, don't call LLM
        if not rule_result.source_valid:
            return rule_result

        # If LLM is disabled, return rule result
        if not self._enabled or not self._api_key:
            logger.debug("LLM disabled or no API key, returning rule result")
            return rule_result

        # Step 2: Call LLM for texts that might contain facts
        if not self._looks_like_has_facts(text):
            logger.debug("Text filtered out by _looks_like_has_facts: %s", text[:50])
            return rule_result

        logger.debug("Calling LLM for text: %s", text[:50])
        llm_result = self._extract_with_llm(text, source_role)
        if llm_result and llm_result.atoms:
            return llm_result

        return rule_result

    def _looks_like_has_facts(self, text: str) -> bool:
        """Heuristic: does this text likely contain extractable facts?"""
        # Skip very short texts
        if len(text.strip()) < 10:
            return False
        # Skip pure questions
        if text.strip().endswith("?") or text.strip().endswith("？"):
            return False
        # Skip greetings
        greetings = ["hello", "hi", "hey", "你好", "hi", "ok", "好的", "嗯"]
        if text.strip().lower() in greetings:
            return False
        return True

    def _extract_with_llm(self, text: str, source_role: str) -> ExtractionResult | None:
        """Call LLM to extract facts."""
        try:
            import httpx

            prompt = EXTRACT_PROMPT.format(text=text, source_role=source_role)

            self._call_count += 1
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 1000,
                },
                timeout=30.0,
            )
            response.raise_for_status()

            content = response.json()["choices"][0]["message"]["content"]
            atoms = self._parse_llm_response(content)

            if not atoms:
                return None

            # Validate each atom
            validated = []
            for atom in atoms:
                if not isinstance(atom, dict):
                    continue
                if "type" not in atom or "content" not in atom:
                    continue
                if atom["type"] not in ("preference", "fact", "plan", "relationship", "event"):
                    continue
                validated.append({
                    "type": atom["type"],
                    "content": atom["content"],
                    "confidence": float(atom.get("confidence", 0.7)),
                })

            if not validated:
                return None

            return ExtractionResult(
                atoms=validated,
                confidence=max(a["confidence"] for a in validated),
                source_valid=True,
            )

        except Exception as e:
            logger.warning("LLM extraction failed: %s", e)
            return None

    def _parse_llm_response(self, content: str) -> list[dict]:
        """Parse LLM response, handling various formats."""
        content = content.strip()

        # Try direct JSON parse
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and "atoms" in parsed:
                return parsed["atoms"]
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code block
        import re
        json_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try to find JSON array in the text
        json_match = re.search(r"\[.*\]", content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        return []


class HybridExtractor:
    """Hybrid extractor: rules + optional LLM.

    Usage:
        extractor = HybridExtractor(use_llm=True)
        result = extractor.extract(text, source_role)
    """

    def __init__(self, use_llm: bool = False, api_key: str | None = None) -> None:
        self._rule_extractor = DeterministicExtractor()
        self._llm_extractor = LLMExtractor(api_key=api_key, enabled=use_llm)

    @property
    def llm_call_count(self) -> int:
        return self._llm_extractor.call_count

    def extract(
        self,
        text: str,
        source_role: str = "user",
        context: dict | None = None,
    ) -> ExtractionResult:
        """Extract using hybrid approach."""
        return self._llm_extractor.extract(text, source_role, context)