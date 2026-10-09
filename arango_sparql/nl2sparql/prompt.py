"""Prompt construction for the NL → SPARQL pipeline.

Per ``.cursor/rules/300-nl2sparql.mdc``:

* The schema context is the **OWL ontology in Turtle** — LLMs read
  Turtle far better than JSON-LD or XML.
* The system prompt pins the dialect to **SPARQL 1.1**, forbids
  vendor extensions, requires fully-qualified IRIs (no bare prefixes
  the model invents), and constrains output to a fenced
  ``\u0060\u0060\u0060sparql`` block.
* Repair attempts append the deterministic translator's error message
  to the *user* turn — the system prompt stays byte-stable so any
  provider-side prefix cache keeps hitting.

Mirror of ``arango_cypher.nl2cypher._core.PromptBuilder`` adapted for
SPARQL. The Cypher project's BM25 few-shot index, tenant guardrails,
and entity-resolution hooks are deliberately out of scope for this
bootstrap module — the rule file lists them as future submodules
(``fewshot.py``, ``tenant_guardrail.py``, …) to be added when the
deterministic translator is far enough along to validate the LLM's
output against a real schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Reserved for future few-shot insertion — see rule 300 §"Prompt construction".
_FEWSHOT_LIMIT = 3

_SYSTEM_PROMPT = """You are a SPARQL 1.1 expert. Given a natural-language question and an OWL ontology in Turtle, generate a single valid SPARQL 1.1 query that answers the question against that ontology.

Rules:
- Use ONLY classes and properties declared in the ontology below or listed in the schema summary.
- Terms in the `phys:` namespace (phys:typeValue, phys:collectionName, phys:mappingStyle, …) are storage annotations that describe how each class is stored. They are NOT data: never use them as predicates.
- Use SPARQL 1.1 syntax (SELECT / WHERE / FILTER / OPTIONAL / UNION / GROUP BY / ORDER BY / LIMIT, etc.).
- Do NOT use vendor extensions (no ArangoDB AQL, no Cypher, no SQL).
- Use fully-qualified IRIs or declare PREFIX directives for every prefix you use. Do NOT invent prefix bindings.
- Return a single SPARQL query — no commentary, no Markdown other than the fenced block.
- Wrap the query in a ```sparql``` fenced code block.

Ontology (Turtle):
{ontology}"""


_REPAIR_USER_SUFFIX = (
    "\n\nYour previous SPARQL query failed downstream translation:\n"
    "{error}\n"
    "Please return a corrected SPARQL 1.1 query in a ```sparql``` fenced block."
)


_EXPLAIN_SYSTEM_PROMPT = """You explain SPARQL 1.1 queries in plain English for non-technical readers.

Rules:
- One short paragraph, no Markdown, no code fences.
- Say what entities are returned and what filters / joins / aggregations apply.
- Do not restate the SPARQL line-by-line; describe the *intent*.
- If the query is empty or syntactically broken, say so plainly."""


@dataclass
class PromptBuilder:
    """Composable builder for the LLM message list.

    Renders one ``system`` turn (mostly cacheable: prelude + ontology)
    and one ``user`` turn (the NL question, optionally suffixed with a
    repair-loop error to feed back to the model).

    Invariants
    ----------
    * Zero-shot, zero-repair case: :meth:`render_system` is byte-identical
      across every call with the same ontology, so any provider-side
      prefix cache hits on the second of two identical translations.
    * Repair context never touches the system message; it is appended to
      the user message only — matching the Cypher project's policy and
      keeping the cache key stable.
    """

    ontology_ttl: str = ""
    schema_summary: str | None = None
    few_shot_examples: list[tuple[str, str]] = field(default_factory=list)
    repair_context: str = ""

    def render_system(self) -> str:
        """Return the system turn — prelude + ontology + optional schema summary."""
        ontology = self._ontology_block()
        base = _SYSTEM_PROMPT.replace("{ontology}", ontology)
        extensions: list[str] = []
        if self.schema_summary:
            extensions.append(self._render_schema_summary_section())
        if self.few_shot_examples:
            extensions.append(self._render_few_shot_section())
        if not extensions:
            return base
        return base + "\n\n" + "\n\n".join(extensions)

    def render_user(self, question: str) -> str:
        """Return the user turn — the NL question plus optional repair suffix."""
        cleaned = (question or "").strip()
        if not self.repair_context:
            return cleaned
        return cleaned + _REPAIR_USER_SUFFIX.format(error=self.repair_context)

    def render_messages(self, question: str) -> list[dict[str, str]]:
        """Return the OpenAI-style ``[{role, content}, …]`` envelope."""
        return [
            {"role": "system", "content": self.render_system()},
            {"role": "user", "content": self.render_user(question)},
        ]

    def _ontology_block(self) -> str:
        return self.ontology_ttl.strip() or "# (no ontology supplied)"

    def _render_schema_summary_section(self) -> str:
        return f"## Schema summary\n{self.schema_summary.strip()}"

    def _render_few_shot_section(self) -> str:
        lines = ["## Examples"]
        for nl, sparql in self.few_shot_examples[:_FEWSHOT_LIMIT]:
            lines.append(f"Q: {nl}")
            lines.append("```sparql")
            lines.append(sparql.strip())
            lines.append("```")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:sparql|sparql11|sparqlv11)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_PREFIX_OR_KEYWORD_RE = re.compile(
    r"^\s*(?:PREFIX\b|BASE\b|SELECT\b|ASK\b|CONSTRUCT\b|DESCRIBE\b|WITH\b|FROM\b)",
    re.IGNORECASE | re.MULTILINE,
)


def extract_sparql_from_response(text: str) -> str:
    """Pull a SPARQL query out of an LLM completion.

    Preference order (mirrors ``arango_cypher.nl2cypher._extract_cypher_from_response``):

    1. The first fenced ``\u0060\u0060\u0060sparql`` block.
    2. Any fenced block (``\u0060\u0060\u0060…\u0060\u0060\u0060``) when the LLM forgot the language tag.
    3. The longest contiguous run of lines that look SPARQL-shaped
       (start with ``PREFIX`` / ``SELECT`` / ``CONSTRUCT`` / …).
    4. The trimmed raw text — last-resort so the parser still gets a
       chance to surface a real error message rather than us masking it
       with an empty string.
    """
    if not text:
        return ""
    fence = _FENCE_RE.search(text)
    if fence:
        return fence.group(1).strip()
    # Generic fence — language tag omitted.
    plain_fence = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if plain_fence:
        return plain_fence.group(1).strip()
    if _PREFIX_OR_KEYWORD_RE.search(text):
        return text.strip()
    return text.strip()


def build_explain_messages(sparql: str) -> list[dict[str, str]]:
    """Return the message list for the ``/nl-explain`` second-pass call.

    Lives here (rather than in ``pipeline.py``) so the explain prompt is
    auditable next to the translation prompt — both surfaces are LLM-
    facing strings the rule file mandates we keep deterministic.
    """
    user = (
        "Explain the following SPARQL 1.1 query in one short paragraph "
        "of plain English:\n\n"
        f"```sparql\n{(sparql or '').strip()}\n```"
    )
    return [
        {"role": "system", "content": _EXPLAIN_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
