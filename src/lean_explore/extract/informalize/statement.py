"""Find the part of a declaration's source that its description depends on.

A description says what a declaration states, not how it is proved. For a
theorem that is everything up to where the proof starts: the doc comment, the
attributes and the statement. For a definition, structure or instance the body
is the meaning, so it is the whole source.

Informalization sends this text to the model and reuses an earlier
description for as long as it does not change, so editing a proof costs
nothing.
"""

import importlib
import logging
from functools import cache
from types import ModuleType

logger = logging.getLogger(__name__)

_PROVED_KINDS = frozenset({"theorem", "lemma"})
"""Declaration kinds whose body is a proof."""


@cache
def _load_parser() -> ModuleType | None:
    """Import the source parser's bindings, or return None without them."""
    try:
        return importlib.import_module("lean_source_parser")
    except ImportError:
        logger.warning(
            "lean_source_parser is not installed; whole declarations, proofs "
            "included, are sent to the model and used to match earlier "
            "descriptions"
        )
        return None


def statement_text(source_text: str) -> str:
    """Return the source of a declaration without the proof of a theorem.

    Args:
        source_text: A declaration as written, with its doc comment and
            attributes.

    Returns:
        The source up to the end of the statement for a theorem or lemma, and
        the whole source for any other declaration, for source that the parser
        does not recognise, and when the parser is not installed.
    """
    parser = _load_parser()
    if parser is None:
        return source_text
    declarations = parser.parse(source_text)
    if not declarations or declarations[0]["kind"] not in _PROVED_KINDS:
        return source_text
    statement_end = declarations[0]["signature"][3]
    return source_text.encode("utf-8")[:statement_end].decode("utf-8", "ignore")
