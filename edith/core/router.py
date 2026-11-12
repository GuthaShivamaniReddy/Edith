"""Router: picks the model for a request. Rules first, model second (PLAN.md section 6).

Phase 1 uses rules only. A Haiku classification call would add 0.5-2 s to every
request, and the rules below cover the clear cases. When unsure, it picks the
stronger model, since a wrong cheap guess costs more than the saving (ADR 0011).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from edith.config import EdithConfig
from edith.core.events import Request, RouteKind


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    model: str
    reason: str


_RESEARCH = re.compile(
    r"\b(research|deep dive|full analysis|analy[sz]e|investigate|compare|comparison|"
    r"pros and cons|literature|in[- ]depth|thorough(ly)?)\b",
    re.IGNORECASE,
)
# Anything that may need a tool, a file, the live web or a multi-step plan.
_TASK = re.compile(
    r"\b(open|close|launch|start|set|turn|change|create|make|write|save|note|remind|"
    r"timer|alarm|find|search|look up|lookup|read|list|show|summari[sz]e|delete|remove|trash|"
    r"move|copy|paste|clipboard|screenshot|screen|volume|mute|unmute|file|files|folder|"
    r"document|download|send|email|calendar|schedule|book|buy|plan|cpu|memory|disk|battery|"
    r"computer|system|latest|news|current|currently|right now|price|"
    r"weather|score|stock|update|install|run|code|script|fix|debug|did you do)\b",
    re.IGNORECASE,
)
_URL_OR_PATH = re.compile(r"(https?://|www\.|[a-z]:\\|~/|/[\w.-]+/|\.\w{2,4}\b)", re.IGNORECASE)
QUICK_MAX_WORDS = 25


def route(request: Request, cfg: EdithConfig) -> Route:
    text = request.text.strip()
    words = len(text.split())

    if _RESEARCH.search(text):
        # Opus deep research with subagents arrives in Phase 6; Sonnet handles it until then.
        return Route("deep_research", cfg.models.main, "research keywords")
    if _TASK.search(text) or _URL_OR_PATH.search(text):
        return Route("task", cfg.models.main, "may need tools or live data")
    if words <= QUICK_MAX_WORDS:
        return Route("quick_answer", cfg.models.fast, "short conversational request")
    return Route("task", cfg.models.main, "long request")
