"""Recording the questions asked under a control plane (its FR-6.7, FR-14.4).

Each route that runs a query calls :func:`record_question` once the query has run and
before answering. Outside a control plane it does nothing. Under one, the question is
recorded in the project's history: who asked (from the context), its words when it was
asked in words, and the SPARQL it ran. A question that cannot be recorded is not answered:
503 ``E_QUESTION_NOT_RECORDED``, and the rows are not returned.

The page's graph view runs the question it just asked again, widened (``graph: true``).
That run is the same question: it is recorded only when this session has not recorded the
same words and query within :data:`AGAIN_SECONDS`, so a caller cannot read through the graph
run unrecorded, and the page's own graph run does not record the question twice.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from fastapi import HTTPException, Request

from . import control_plane

#: How long after a question is recorded a re-run of it for the graph view is the same ask.
AGAIN_SECONDS = 600.0

NOT_RECORDED = "E_QUESTION_NOT_RECORDED"


def record_question(
    request: Request,
    session: Any,
    *,
    query: str,
    question: str | None = None,
    again: bool = False,
) -> None:
    """Record *query* (asked as *question*, or written) for the request's context. With
    *again*, a re-run of a question this session recorded recently is not recorded twice.
    Raises :class:`HTTPException` 503 when it cannot be recorded."""
    if not control_plane.mode():
        return
    recent: dict[str, float] | None = getattr(session, "recorded_questions", None)
    key = hashlib.sha256(f"{question or ''}\0{query}".encode()).hexdigest()
    now = time.monotonic()
    if again and recent is not None and now - recent.get(key, float("-inf")) < AGAIN_SECONDS:
        return
    try:
        control_plane.record_question(request.headers, question=question, query=query)
    except control_plane.QuestionNotRecorded as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "The question could not be recorded in the project's history, so it "
                f"is not answered: {exc}",
                "code": NOT_RECORDED,
            },
        ) from exc
    if recent is not None:
        for old in [k for k, at in recent.items() if now - at >= AGAIN_SECONDS]:
            del recent[old]
        recent[key] = now
