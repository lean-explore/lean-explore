"""Run source extraction on small Lean packages with known references.

Every case under ``cases/`` is a package whose expected references were read
from Lean itself (the constants of each declaration's elaborated type that
are declared in the package or listed as known). The cases need the parser's
Python bindings and are skipped without them.
"""

import json
from pathlib import Path

import pytest

CASES_DIRECTORY = Path(__file__).parent / "cases"

KNOWN_LIMITS = {
    # Structure parents are recorded on purpose: Lean's type of a structure
    # does not mention them, but they are what the structure depends on.
    "extends-clause-not-in-type": "structure parents are recorded on purpose",
    "extends-not-in-type": "structure parents are recorded on purpose",
    "extends-parents-not-in-type": "structure parents are recorded on purpose",
    "named-parent-label-counted-as-a-constant": "named parent labels are not read",
    # An alias is recorded as depending on its target rather than on the
    # constants of the target's type.
    "alias-type-is-target-type": "an alias refers to its target",
    "alias-signature-is-target-name": "an alias refers to its target",
    # Fields, constructors and local helpers are not stored, so they shadow
    # other names but are never a reference.
    "field-projection-shadows-root": "fields are not stored",
    "explicit-field-reference": "fields are not stored",
    "qualified-field-falls-back-to-structure": "fields are not stored",
    "explicit-mk-axiom-dropped": "names ending in .mk are not stored",
    # A field found by the type of its term is not stored either, nor is the
    # parent projection that an inherited field goes through.
    "field-notation-on-global-def": "fields are not stored",
    "field-inherited-through-extends": "parent projections are not stored",
    # A field is found only if the type of its term is written.
    "field-unknown-receiver-type": "the receiver's type is not written",
    # Leading-dot names need the expected type.
    "leading_dot": "leading-dot names need the expected type",
    "leading-dot-identifier-dropped": "leading-dot names need the expected type",
    # Lean chooses among equal candidates by type.
    "atomic_root_and_open": "overloads are chosen by type",
    "open_from_ambiguity_multiple": "overloads are chosen by type",
    "notation-overloaded-token": "overloads are chosen by type",
    # Forms that are not read.
    "match-pattern-var-in-type": "match patterns are not read",
    "term_open_in": "term-level open is not read",
    "mutual_different_namespace_suffixes": "the shared namespace of a mutual "
    "block is not entered",
    "section_variable_type": "axioms take only the variables they use",
    "duplicate-name-uses-unimported-definition": "a name declared twice keeps "
    "its first declaration",
    # What the parser reports for these declarations has no usable signature.
    "structure-custom-ctor": "the parser gives no signature",
    "custom-structure-constructor": "the parser gives no signature",
    "custom-structure-constructor-signature": "the parser gives no signature",
    "generated-ext-theorems-read-attribute-as-type": "generated declarations "
    "have no signature",
    "named-macro-body-counted-as-parser-type": "macros have no signature",
}
"""Cases that source extraction is known to get wrong, with the reason."""


def _load_cases() -> list:
    """Collect every case as a pytest parameter, marking the known limits."""
    parameters = []
    for path in sorted(CASES_DIRECTORY.glob("*.json")):
        for case in json.loads(path.read_text(encoding="utf-8")):
            marks = []
            if case["id"] in KNOWN_LIMITS:
                reason = KNOWN_LIMITS[case["id"]]
                marks.append(pytest.mark.xfail(reason=reason, strict=True))
            identifier = f"{path.stem}:{case['id']}"
            parameters.append(pytest.param(case, id=identifier, marks=marks))
    return parameters


@pytest.mark.parametrize("case", _load_cases())
def test_case(case, tmp_path):
    """The references found for a case match the ones Lean reports."""
    pytest.importorskip("lean_source_parser")
    from lean_explore.extract.source.reader import parse_package_sources

    root = tmp_path / "pkg"
    for relative_path, text in case["files"].items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    rows = parse_package_sources(
        root, "https://example.com/", lambda module: True, known_names=case["known"]
    )
    found = {row.name: sorted(row.dependencies or []) for row in rows}
    assert {name: found.get(name) for name in case["expected"]} == case["expected"]
