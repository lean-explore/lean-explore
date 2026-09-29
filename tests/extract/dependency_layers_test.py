"""Tests for ordering declarations by their dependencies."""

import pytest

from lean_explore.extract.dependency_layers import (
    build_dependency_layers,
    parse_dependencies,
)
from tests.extract.builders import make_declaration


def _layer_names(declarations) -> list[list[str]]:
    return [[d.name for d in layer] for layer in build_dependency_layers(declarations)]


class TestParseDependencies:
    """Tests for parse_dependencies."""

    @pytest.mark.parametrize(
        ("dependencies", "expected"),
        [
            ('["Nat", "List"]', ["Nat", "List"]),
            (["Nat", "List"], ["Nat", "List"]),
            ("[]", []),
            ("", []),
            (None, []),
        ],
    )
    def test_parse(self, dependencies, expected):
        """JSON strings and lists are accepted; empty values give []."""
        assert parse_dependencies(dependencies) == expected


class TestBuildDependencyLayers:
    """Tests for build_dependency_layers."""

    def test_dependencies_come_in_earlier_layers(self):
        """Each declaration is placed one layer after its deepest dependency."""
        declarations = [
            make_declaration("Top", dependencies=["Mid", "Base"]),
            make_declaration("Mid", dependencies=["Base"]),
            make_declaration("Base"),
            make_declaration("Other"),
        ]

        assert _layer_names(declarations) == [["Base", "Other"], ["Mid"], ["Top"]]

    def test_dependencies_outside_the_input_are_ignored(self):
        """Unknown dependency names do not delay a declaration."""
        declarations = [make_declaration("A", dependencies=["Missing"])]

        assert _layer_names(declarations) == [["A"]]

    def test_cycles_and_their_dependents_form_a_final_layer(self):
        """Declarations stuck behind a cycle are appended together at the end."""
        declarations = [
            make_declaration("Free"),
            make_declaration("A", dependencies=["B", "Free"]),
            make_declaration("B", dependencies=["A"]),
            make_declaration("UsesCycle", dependencies=["A"]),
        ]

        assert _layer_names(declarations) == [["Free"], ["A", "B", "UsesCycle"]]

    def test_empty_input(self):
        """No declarations give no layers."""
        assert build_dependency_layers([]) == []
