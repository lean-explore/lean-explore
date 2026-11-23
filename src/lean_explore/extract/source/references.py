"""Find the declarations a statement refers to without elaborating it.

The doc-gen4 path stores, as a declaration's dependencies, the constants that
appear in its elaborated type. Source extraction has no elaborated type, so
this module reads the identifiers written in the signature and resolves them
the way Lean resolves a name: against the namespaces of the declaration, the
root namespace and the namespaces opened at that point of the file.

The result is an approximation. References made through notation (``ℝ``,
``+``, ``∈``), through field access on a term (``s.card``) and through
elaboration (instances, coercions) are not found. Where Lean would choose
between several candidates by their types, no reference is recorded.
"""

from collections.abc import Callable, Collection, Iterable

from lean_explore.extract.source.lexer import Group, Token, nest, tokenize
from lean_explore.extract.source.names import NameTable, Use, resolve
from lean_explore.extract.source.scopes import (
    Scope,
    Variable,
    binder_names,
    colon_index,
    group_binder_names,
    instance_key,
    is_plain,
)

_BINDER_SYMBOLS = frozenset("∀∃λΣΠ∑∏⋃⋂⨆⨅∫∮")


_BINDER_WORDS = frozenset({"fun", "forall", "exists"})


_BINDER_ENDS = frozenset({",", "=>", "↦"})


_PI_ARROWS = frozenset({"→", "->", "×"})


_SET_BUILDER_MARKS = frozenset({"|", "//"})


_SET_BUILDER_STARTS = frozenset({":", "∈", "|", "//"})


_HEAD_ENDS = frozenset({"extends", "where"})


_LOCAL_DEFINITIONS = frozenset({"let", "have"})


_BINDER_STOP_WORDS = frozenset({"in"})


_HEADLESS_KINDS = frozenset({"parent_projection"})


_UNIVERSE_WORDS = frozenset({"Type", "Sort"})


_LEVEL_ENDS = frozenset({"→", "->", ",", ":=", "=", "×"})


_HEADER_ONLY_KINDS = frozenset({"theorem", "lemma"})


_TYPE_KINDS = frozenset({"structure", "class", "inductive", "class_inductive"})


_BINDER_MARKS = frozenset({"'", "!", "ᶠ", "ˢ"})
_SYNTAX_KINDS = frozenset(
    {"macro", "elab", "syntax", "notation", "simproc", "config_elab", "unif_hint"}
)
"""Kinds whose signature is syntax, not a type."""
_VALUE_KINDS = frozenset({"initialize", "register_option"})
"""Kinds whose signature runs on into a value."""
_VALUE_STARTS = frozenset({":=", "←"})


def _is_symbol(item: Token | Group | None, symbols: Collection[str]) -> bool:
    """Tell whether an item is one of the given symbols."""
    return isinstance(item, Token) and not item.is_identifier and item.text in symbols


def _is_word(item: Token | Group | None, words: Collection[str]) -> bool:
    """Tell whether an item is one of the given identifiers."""
    return isinstance(item, Token) and item.is_identifier and item.text in words


def _find(items: list[Token | Group], start: int, symbols: Collection[str]) -> int:
    """Return the index of the first of the symbols from ``start``, or -1."""
    for index in range(start, len(items)):
        if _is_symbol(items[index], symbols):
            return index
    return -1


def _is_binder_name(item: Token | Group) -> bool:
    """Tell whether an item after ``∀`` or ``fun`` is still a binder."""
    if isinstance(item, Group):
        return True
    return is_plain(item) and not _is_word(item, _BINDER_STOP_WORDS)


class _Walker:
    """Collects the references of one signature, tracking local names."""

    def __init__(
        self,
        resolve_identifier: Callable[[str], str | None],
        variables: Collection[str] = (),
    ):
        """Start with no references."""
        self._resolve = resolve_identifier
        self._variables = variables
        self.references: dict[str, None] = {}
        self.used_variables: set[str] = set()

    def walk(
        self,
        items: list[Token | Group],
        bound: frozenset[str],
        labels: bool = False,
    ) -> None:
        """Read items left to right; binders bind to the end of the items.

        Args:
            items: The items to read.
            bound: Local names in scope.
            labels: Whether the items are the whole content of a bracket,
                where ``x := v`` names an argument or field.
        """
        index = 0
        while index < len(items):
            item = items[index]
            following = items[index + 1] if index + 1 < len(items) else None
            if _is_symbol(item, {":="}) and _is_word(following, {"by"}):
                # The tactic of an automatic argument is not part of the type.
                return
            if isinstance(item, Group):
                bound = self._group(item, following, bound)
            elif _is_symbol(item, _BINDER_SYMBOLS) or _is_word(item, _BINDER_WORDS):
                index, bound = self._binder(items, index, bound)
            elif (
                _is_word(item, _LOCAL_DEFINITIONS) and following and is_plain(following)
            ):
                assert isinstance(following, Token)
                bound = bound | {following.text}
                index += 1
            elif _is_word(item, _UNIVERSE_WORDS):
                # What follows ``Type`` or ``Sort`` is a universe level.
                index += following is not None and not _is_symbol(
                    following, _LEVEL_ENDS
                )
            elif item.is_identifier and not (labels and self._is_label(items, index)):
                self._identifier(item, bound)
            index += 1

    @staticmethod
    def _is_label(items: list[Token | Group], index: int) -> bool:
        """Tell whether an identifier names an argument or field, as ``(x := v)``."""
        if index + 1 >= len(items) or not _is_symbol(items[index + 1], {":="}):
            return False
        return index == 0 or _is_symbol(items[index - 1], {","})

    def _identifier(self, token: Token, bound: frozenset[str]) -> None:
        """Record the declaration an identifier refers to, if it is global."""
        first = token.text.split(".")[0]
        if token.after_dot or first in bound:
            return
        if first in self._variables:
            self.used_variables.add(first)
            return
        resolved = self._resolve(token.text)
        if resolved is not None:
            self.references[resolved] = None

    def _group(
        self, group: Group, following: Token | Group | None, bound: frozenset[str]
    ) -> frozenset[str]:
        """Read a bracket group; return the names bound after it."""
        if group.opening == ".{":
            return bound
        if group.opening == "{" and _find(group.items, 0, _SET_BUILDER_MARKS) >= 0:
            end = _find(group.items, 0, _SET_BUILDER_STARTS)
            inner = bound | set(binder_names(group.items[:end]))
            self.walk(group.items[end:], inner)
            return bound
        colon = colon_index(group.items)
        if colon is not None and _is_symbol(following, _PI_ARROWS):
            self.walk(group.items[colon + 1 :], bound)
            return bound | set(group_binder_names(group))
        self.walk(group.items, bound, labels=True)
        return bound

    def _binder(
        self, items: list[Token | Group], index: int, bound: frozenset[str]
    ) -> tuple[int, frozenset[str]]:
        """Read the binders after ``∀``, ``fun`` and the like.

        Returns:
            The index of the binders' last item and the names bound after it.
        """
        end = _find(items, index + 1, _BINDER_ENDS)
        if end < 0:
            return index, bound
        binders = items[index + 1 : end]
        while binders and _is_symbol(binders[0], _BINDER_MARKS):
            binders = binders[1:]
        count = 0
        while count < len(binders) and _is_binder_name(binders[count]):
            count += 1
        return end, self.binders(binders[:count], binders[count:], bound)

    def binders(
        self,
        binders: list[Token | Group],
        annotation: list[Token | Group],
        bound: frozenset[str],
    ) -> frozenset[str]:
        """Read binders and their types; return the names bound after them."""
        for binder in binders:
            if isinstance(binder, Group):
                colon = colon_index(binder.items)
                if colon is not None:
                    self.walk(binder.items[colon + 1 :], bound)
                elif binder.opening == "[":
                    self.walk(binder.items, bound)
                bound = bound | set(group_binder_names(binder))
        self.walk(annotation, bound)
        return bound | set(binder_names(binders))


def _head_end(items: list[Token | Group]) -> int:
    """Return where a signature's name and binders end and its type begins."""
    for index, item in enumerate(items):
        if _is_symbol(item, {":", ":="}) or _is_word(item, _HEAD_ENDS):
            return index
    return len(items)


def _head_binders(items: list[Token | Group], name: str) -> list[Token | Group]:
    """Return the binders of a signature's head.

    Bracketed binders always count. Bare names count only after the
    declaration's own name, so keywords and the name itself are not bound.
    """
    head = items[: _head_end(items)]
    for index, item in enumerate(head):
        if isinstance(item, Token) and item.is_identifier:
            if name and (name == item.text or name.endswith("." + item.text)):
                groups = [item for item in head[:index] if isinstance(item, Group)]
                return groups + head[index + 1 :]
    return [item for item in head if isinstance(item, Group)]


def _mentioned(variable: Variable, names: Collection[str]) -> set[str]:
    """Return the variables that a variable's type mentions."""

    def collect(items: Iterable[Token | Group]) -> set[str]:
        found: set[str] = set()
        for item in items:
            if isinstance(item, Group):
                found |= collect(item.items)
            elif is_plain(item) and item.text in names:
                found.add(item.text)
        return found

    return collect(variable.type_items)


def _include_variables(
    walker: _Walker, scope: Scope, used: set[str], instances: bool
) -> None:
    """Add the references of the section variables a declaration includes.

    A named variable is included when the declaration uses it, directly or
    through the type of another included variable. An instance variable is
    included when every variable its type mentions is included, unless
    ``omit`` leaves it out; a type declaration takes none.
    """
    variables = scope.variables
    by_name = {name: variable for variable in variables for name in variable.names}
    walked: set[int] = set()
    waiting = sorted(used)
    while waiting:
        variable = by_name.get(waiting.pop())
        if variable is None or id(variable) in walked:
            continue
        walked.add(id(variable))
        walker.walk(list(variable.type_items), frozenset())
        waiting.extend(walker.used_variables - used)
        used |= walker.used_variables
    for variable in variables if instances else ():
        if not variable.is_instance or id(variable) in walked:
            continue
        omitted = instance_key(variable.type_items) in scope.omitted
        if not omitted and _mentioned(variable, by_name) <= used:
            walker.walk(list(variable.type_items), frozenset())


def find_references(
    signature: str,
    scope: Scope,
    table: NameTable,
    use: Use,
    kind: str = "",
    bound: Iterable[str] = (),
    body: str = "",
) -> list[str] | None:
    """List the known declarations that a signature refers to.

    Args:
        signature: The declaration's signature as written.
        scope: The scope in effect at the declaration.
        table: The declarations that can be referred to.
        use: Where the declaration is written.
        kind: The declaration's kind as the parser reports it.
        bound: Further local names, such as the binders of the structure a
            field belongs to.
        body: The whole declaration as written. A definition includes the
            section variables its body uses; a theorem only those of its
            signature.

    Returns:
        The referenced names in order of first appearance, without the
        declaration itself, or None if there are none.
    """
    if kind in _SYNTAX_KINDS:
        return None
    items = nest(tokenize(signature))
    for index, item in enumerate(items):
        # A deriving clause or a value on the same line is not the type.
        value = kind in _VALUE_KINDS and _is_symbol(item, _VALUE_STARTS)
        if _is_word(item, {"deriving"}) or value:
            items = items[:index]
            break
    variables = {name for variable in scope.variables for name in variable.names}
    walker = _Walker(lambda identifier: resolve(identifier, table, use), variables)
    local_names = frozenset(bound)
    head = 0 if kind in _HEADLESS_KINDS else _head_end(items)
    if head:
        binders = _head_binders(items, use.name)
        local_names = walker.binders(binders, [], local_names)
    walker.walk(items[head:], local_names)
    used = walker.used_variables | (scope.included & variables)
    if body and kind not in _HEADER_ONLY_KINDS and variables:
        used |= {
            token.text
            for token in tokenize(body)
            if token.is_identifier
            and not token.after_dot
            and token.text.split(".")[0] in variables
        }
        used = {name.split(".")[0] for name in used}
    if scope.variables:
        used -= scope.omitted
        _include_variables(walker, scope, set(used), kind not in _TYPE_KINDS)
    walker.references.pop(use.name, None)
    return list(walker.references) or None


def signature_binder_names(signature: str, name: str) -> list[str]:
    """Return the names bound by the binders of a signature.

    Args:
        signature: A declaration's signature as written.
        name: Fully qualified name of the declaration.

    Returns:
        The names of its binders.
    """
    return binder_names(_head_binders(nest(tokenize(signature)), name))
