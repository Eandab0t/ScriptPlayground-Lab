"""Canonical project-state boundary for Components V2 designs."""

from __future__ import annotations

COMPONENT_TYPES = {1, 2, 3, 9, 10, 11, 12, 13, 14, 17}


def validate_project(project: object) -> dict:
    if not isinstance(project, dict):
        raise TypeError("project must be an object")
    if project.get("version") != 1:
        raise ValueError("project.version must be 1")
    tree = project.get("tree")
    if not isinstance(tree, list) or not tree:
        raise ValueError("project.tree must be a non-empty array")
    for index, node in enumerate(tree):
        _validate_node(node, f"project.tree[{index}]")
    return project


def _validate_node(node: object, path: str) -> None:
    if not isinstance(node, dict):
        raise TypeError(f"{path} must be an object")
    kind = node.get("type")
    if not isinstance(kind, int) or kind not in COMPONENT_TYPES:
        raise ValueError(f"{path}.type is not a supported component type")
    children = node.get("components")
    if children is not None:
        if not isinstance(children, list):
            raise ValueError(f"{path}.components must be an array")
        for index, child in enumerate(children):
            _validate_node(child, f"{path}.components[{index}]")
    items = node.get("items")
    if items is not None and not isinstance(items, list):
        raise ValueError(f"{path}.items must be an array")
    if isinstance(items, list):
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise TypeError(f"{path}.items[{index}] must be an object")
    accessory = node.get("accessory")
    if accessory is not None:
        _validate_node(accessory, f"{path}.accessory")
