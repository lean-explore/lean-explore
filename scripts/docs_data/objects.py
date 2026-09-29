"""Serialization of griffe functions, classes, attributes, and modules."""

import pathlib

from griffe import (
    AliasResolutionError,
    Attribute,
    Class,
    Decorator,
    ExprCall,
    Function,
    Module,
    Object,
    Parameters,
)

from .docstrings import parse_docstring, resolve_annotation
from .schema import (
    AttributeDict,
    ClassDict,
    DecoratorDict,
    DocstringAttributeDict,
    DocstringSections,
    FunctionDict,
    ModuleDict,
    ParameterDict,
    ReturnDict,
)

# --- Shared Helpers ---


def is_private_name(name: str) -> bool:
    """Checks for a single-underscore private name; dunder names are public."""
    return name.startswith("_") and not name.startswith("__")


def relative_filepath(griffe_object: Object) -> str | None:
    """Returns the object's file path relative to the working directory."""
    if not griffe_object.filepath:
        return None
    return str(griffe_object.filepath.relative_to(pathlib.Path.cwd()))


def line_span(griffe_object: Object) -> list[int]:
    """Returns ``[lineno, endlineno]`` when both are known, else an empty list."""
    if griffe_object.lineno and griffe_object.endlineno:
        return [griffe_object.lineno, griffe_object.endlineno]
    return []


def docstring_value(griffe_object: Object) -> str:
    """Returns the raw docstring text of an object, or an empty string."""
    return griffe_object.docstring.value if griffe_object.docstring else ""


# --- Signature Serialization ---


def serialize_typer_option(function_call: ExprCall) -> str:
    """Formats a typer.Option call as a multi-line string."""
    function_name = str(function_call.function)
    arguments: list[str] = []

    if hasattr(function_call, "arguments"):
        arguments = [str(argument) for argument in function_call.arguments]

    if not arguments:
        return f"{function_name}()"

    indent = "    "
    formatted_arguments = f",\n{indent}".join(arguments)
    return f"{function_name}(\n{indent}{formatted_arguments}\n)"


def format_default(default: object) -> str | None:
    """Formats a parameter default value, with special handling for typer.Option."""
    if default is None:
        return None
    if (
        isinstance(default, ExprCall)
        and hasattr(default, "function")
        and str(default.function) == "typer.Option"
    ):
        return serialize_typer_option(default)
    return str(default)


def serialize_parameters(parameters: Parameters) -> list[ParameterDict]:
    """Converts griffe Parameters into serializable dictionaries."""
    if not parameters:
        return []
    return [
        {
            "name": parameter.name,
            "annotation": resolve_annotation(parameter.annotation),
            "kind": parameter.kind.value,
            "default": format_default(parameter.default),
        }
        for parameter in parameters
    ]


def serialize_decorators(decorators: list[Decorator]) -> list[DecoratorDict]:
    """Converts griffe Decorator objects into serializable dictionaries."""
    return [
        {
            "text": str(decorator.value),
            "path": decorator.callable_path,
            "lineno": getattr(decorator, "lineno", None),
            "endlineno": getattr(decorator, "endlineno", None),
        }
        for decorator in decorators
    ]


def merge_parameter_descriptions(
    code_parameters: list[ParameterDict], docstring_parameters: list[ParameterDict]
) -> None:
    """Merges docstring descriptions into code parameters in-place."""
    docstring_map = {parameter["name"]: parameter for parameter in docstring_parameters}

    for code_parameter in code_parameters:
        if code_parameter["name"] in docstring_map:
            code_parameter["description"] = docstring_map[code_parameter["name"]].get(
                "description", ""
            )


def build_returns_info(
    function: Function, docstring_sections: DocstringSections
) -> ReturnDict:
    """Builds return information combining code annotation and docstring description."""
    returns_info = {
        "annotation": resolve_annotation(function.returns),
        "description": "",
    }

    docstring_returns = docstring_sections.get("returns")
    if isinstance(docstring_returns, dict):
        returns_info["description"] = docstring_returns.get("description", "")
        if docstring_returns.get("annotation"):
            returns_info["annotation"] = docstring_returns.get("annotation")
    elif isinstance(docstring_returns, list) and docstring_returns:
        first_return = docstring_returns[0]
        returns_info["description"] = first_return.get("description", "")
        if first_return.get("annotation"):
            returns_info["annotation"] = first_return.get("annotation")
        if len(docstring_returns) > 1:
            returns_info["description"] += " (Multiple return paths documented)"

    return returns_info


# --- Function Serialization ---


def serialize_function(function: Function) -> FunctionDict:
    """Converts a griffe Function into a serializable dictionary.

    Includes full documentation from both code and docstrings.
    """
    docstring_sections = parse_docstring(function.docstring)
    code_parameters = serialize_parameters(function.parameters)

    # Merge docstring parameter descriptions into code parameters
    merge_parameter_descriptions(
        code_parameters, docstring_sections.get("parameters", [])
    )

    return {
        "name": function.name,
        "path": function.canonical_path,
        "docstring": docstring_value(function),
        "docstring_sections": docstring_sections,
        "parameters": code_parameters,
        "returns": build_returns_info(function, docstring_sections),
        "decorators": serialize_decorators(function.decorators),
        "is_async": getattr(function, "is_async", False),
        "filepath": relative_filepath(function),
        "lineno": function.lineno,
        "lines": line_span(function),
    }


# --- Class Serialization ---


def serialize_attribute(attribute: Attribute) -> AttributeDict:
    """Converts a griffe class Attribute into a serializable dictionary."""
    return {
        "name": attribute.name,
        "value": str(attribute.value) if attribute.value is not None else None,
        "annotation": resolve_annotation(attribute.annotation),
        "docstring": docstring_value(attribute),
        "path": attribute.canonical_path,
        "filepath": relative_filepath(attribute),
        "lineno": attribute.lineno,
    }


def collect_class_members(
    class_object: Class,
) -> tuple[list[FunctionDict], list[AttributeDict]]:
    """Serializes the public methods and attributes defined on a class.

    Args:
        class_object: The griffe class whose members are serialized.

    Returns:
        A ``(methods, attributes)`` pair in class member order. Methods that are
        aliases to unresolvable external imports are skipped.
    """
    methods: list[FunctionDict] = []
    attributes: list[AttributeDict] = []

    for member in class_object.members.values():
        if is_private_name(member.name):
            continue

        if member.is_attribute:
            attributes.append(serialize_attribute(member))
        elif member.is_function:
            try:
                actual_method = member.final_target if member.is_alias else member
                if actual_method:
                    methods.append(serialize_function(actual_method))
            except AliasResolutionError:
                # External imports cannot be resolved, skip them
                continue

    return methods, attributes


def merge_docstring_attributes(
    code_attributes: list[AttributeDict],
    docstring_attributes: list[DocstringAttributeDict],
    class_path: str,
) -> None:
    """Merges docstring-only attributes with code attributes.

    Adds attributes that only appear in docstrings, and fills in
    missing docstrings for code attributes.
    """
    existing_names = {attribute["name"] for attribute in code_attributes}

    for docstring_attribute in docstring_attributes:
        name = docstring_attribute["name"]

        if is_private_name(name):
            continue

        if name not in existing_names:
            # Add attribute that only exists in docstring
            code_attributes.append(
                {
                    "name": name,
                    "value": None,
                    "annotation": docstring_attribute.get("annotation", ""),
                    "docstring": docstring_attribute.get("description", ""),
                    "path": f"{class_path}.{name}",
                    "filepath": None,
                    "lineno": None,
                }
            )
        else:
            # Fill in docstring for existing code attribute
            for code_attribute in code_attributes:
                if code_attribute["name"] == name and not code_attribute["docstring"]:
                    code_attribute["docstring"] = docstring_attribute.get(
                        "description", ""
                    )
                    break


def serialize_class(class_object: Class) -> ClassDict:
    """Converts a griffe Class into a serializable dictionary.

    Includes full documentation from both code and docstrings.
    """
    methods, attributes = collect_class_members(class_object)

    docstring_sections = parse_docstring(class_object.docstring)
    merge_docstring_attributes(
        attributes,
        docstring_sections.get("attributes", []),
        class_object.canonical_path,
    )

    return {
        "name": class_object.name,
        "path": class_object.canonical_path,
        "docstring": docstring_value(class_object),
        "docstring_sections": docstring_sections,
        "methods": sorted(methods, key=lambda x: (x["name"] != "__init__", x["name"])),
        "attributes": sorted(attributes, key=lambda x: x["name"]),
        "bases": [resolve_annotation(base) for base in class_object.bases],
        "filepath": relative_filepath(class_object),
        "lineno": class_object.lineno,
        "lines": line_span(class_object),
    }


# --- Module Serialization ---


def get_definition_module_path(object: Function | Class | Module) -> str:
    """Determines the canonical module path where an object is defined."""
    if (
        hasattr(object, "parent")
        and object.parent
        and hasattr(object.parent, "canonical_path")
    ):
        return object.parent.canonical_path
    elif "." in object.canonical_path:
        return object.canonical_path.rsplit(".", 1)[0]
    else:
        return object.canonical_path


def serialize_module(module: Module) -> ModuleDict:
    """Converts a griffe Module into a serializable dictionary.

    Only includes functions and classes defined directly in this module,
    not imported ones.
    """
    functions = []
    classes = []
    current_path = module.canonical_path

    for member in module.members.values():
        try:
            # Resolve aliases to their targets
            target = member.final_target if member.is_alias else member

            if not target or not isinstance(target.canonical_path, str):
                continue

            definition_path = get_definition_module_path(target)

            if is_private_name(target.name):
                continue

            # Only include items defined in this module
            if target.is_function and isinstance(target, Function):
                if definition_path == current_path:
                    functions.append(serialize_function(target))
            elif target.is_class and isinstance(target, Class):
                if definition_path == current_path:
                    classes.append(serialize_class(target))

        except AliasResolutionError:
            # External imports cannot be resolved, skip them
            continue

    return {
        "name": module.name,
        "path": module.canonical_path,
        "filepath": relative_filepath(module),
        "docstring": docstring_value(module),
        "docstring_sections": parse_docstring(module.docstring),
        "functions": sorted(functions, key=lambda x: x["name"]),
        "classes": sorted(classes, key=lambda x: x["name"]),
        "lineno": module.lineno,
    }
