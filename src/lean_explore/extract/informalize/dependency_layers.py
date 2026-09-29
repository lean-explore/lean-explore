"""Order declarations so that dependencies are processed before dependents."""

import json
import logging
from collections import defaultdict

from lean_explore.models import Declaration

logger = logging.getLogger(__name__)


def parse_dependencies(dependencies: str | list[str] | None) -> list[str]:
    """Parse dependencies field which may be JSON string or list.

    Args:
        dependencies: Dependencies as JSON string, list, or None

    Returns:
        List of dependency names
    """
    if not dependencies:
        return []
    if isinstance(dependencies, str):
        return json.loads(dependencies)
    return dependencies


def build_dependency_layers(
    declarations: list[Declaration],
) -> list[list[Declaration]]:
    """Build dependency layers where each layer has no dependencies on later layers.

    Returns a list of layers, where layer 0 has no dependencies, layer 1 only
    depends on layer 0, etc. Dependencies outside ``declarations`` are ignored.
    Declarations in or behind a cycle are appended as one final layer.

    Args:
        declarations: Declarations to order.

    Returns:
        List of layers, each a list of declarations.
    """
    name_to_declaration = {
        declaration.name: declaration for declaration in declarations
    }
    dependents: defaultdict[str, list[str]] = defaultdict(list)
    in_degree = {declaration.name: 0 for declaration in declarations}

    for declaration in declarations:
        for dependency_name in parse_dependencies(declaration.dependencies):
            if dependency_name in name_to_declaration:
                dependents[dependency_name].append(declaration.name)
                in_degree[declaration.name] += 1

    # Kahn's algorithm, one layer at a time
    layers = []
    current_layer = [name_to_declaration[n] for n in in_degree if in_degree[n] == 0]
    while current_layer:
        layers.append(current_layer)
        next_layer = []
        for declaration in current_layer:
            for dependent in dependents[declaration.name]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    next_layer.append(name_to_declaration[dependent])
        current_layer = next_layer

    remaining = [name_to_declaration[n] for n in in_degree if in_degree[n] > 0]
    if remaining:
        logger.warning(
            "Found %d declarations in cycles, adding as final layer", len(remaining)
        )
        layers.append(remaining)

    return layers
