"""``indent_aql`` is the one two-space indent for nested AQL blocks.

It replaced eleven written-out copies (two named ``_indent``) across the
translator (code-quality review of #196–#199). The cross-test interpreter
reads nested blocks by this layout, so its exact shape is pinned here.
"""

from __future__ import annotations

from arango_sparql.translate import copies, minus_exists
from arango_sparql.translate.builder import AqlQueryBuilder, indent_aql


def test_every_line_is_indented_two_spaces_with_no_trailing_newline() -> None:
    assert indent_aql("FOR d IN c\nRETURN d") == "  FOR d IN c\n  RETURN d"
    assert indent_aql("FOR d IN c\nRETURN d\n") == "  FOR d IN c\n  RETURN d"


def test_a_blank_line_is_indented_too() -> None:
    # textwrap.indent would leave it bare; the emitted AQL never had that.
    assert indent_aql("a\n\nb") == "  a\n  \n  b"


def test_empty_text_is_empty() -> None:
    assert indent_aql("") == ""


def test_nested_blocks_indent_once_per_level() -> None:
    assert indent_aql(indent_aql("RETURN 1")) == "    RETURN 1"


def test_union_arms_use_it() -> None:
    builder = AqlQueryBuilder()
    builder.for_union("row", ["FOR a IN x\nRETURN a", "FOR b IN y\nRETURN b"])
    builder.return_scalar("row")
    aql, _bind_vars = builder.finalize()
    assert "(\n  FOR a IN x\n  RETURN a\n),\n(\n  FOR b IN y\n  RETURN b\n)" in aql


def test_no_module_keeps_a_private_copy() -> None:
    assert not hasattr(minus_exists, "_indent")
    assert not hasattr(copies, "_indent")
