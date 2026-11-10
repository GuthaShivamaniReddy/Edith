from __future__ import annotations

import re
from datetime import UTC, datetime

from edith.config import EdithConfig
from edith.core.prompts import render_system_prompt


def test_renders_persona_and_context(cfg: EdithConfig) -> None:
    text = render_system_prompt(
        cfg, source="telegram", now=datetime(2026, 11, 10, 19, 30, tzinfo=UTC)
    )
    assert "You are EDITH" in text
    assert "Tester's computer" in text
    assert 'Address the user as "boss"' in text
    assert "Tuesday, November 10, 2026, 02:30 PM" in text  # converted to New York time
    assert "Active client: phone" in text
    assert "Location: unknown" in text


def test_no_unfilled_placeholders(cfg: EdithConfig) -> None:
    text = render_system_prompt(cfg)
    assert not re.search(r"\{[a-z_]+\}", text)


def test_phase0_prompt_does_not_claim_tools(cfg: EdithConfig) -> None:
    assert "You have no tools yet" in render_system_prompt(cfg)
