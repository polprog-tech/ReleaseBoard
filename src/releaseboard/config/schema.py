"""JSON Schema validation for configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_PATH = Path(__file__).parent / "schema.json"


class ConfigValidationError(Exception):
    """Raised when configuration fails schema validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        summary = "; ".join(errors[:5])
        if len(errors) > 5:
            summary += f" ... and {len(errors) - 5} more"
        super().__init__(f"Configuration validation failed: {summary}")


_SCHEMA_CACHE: dict[str, Any] | None = None


def _load_schema() -> dict[str, Any]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE


def validate_config(data: dict[str, Any]) -> list[str]:
    """Validate config data against the JSON Schema.

    Returns a list of error messages. Empty list means valid.
    """
    schema = _load_schema()
    validator = jsonschema.Draft7Validator(schema)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{path}: {error.message}")
    return errors


def validate_config_strict(data: dict[str, Any]) -> None:
    """Validate and raise on first batch of errors."""
    errors = validate_config(data)
    if errors:
        raise ConfigValidationError(errors)


def validate_layer_references(data: dict[str, Any]) -> list[str]:
    """Check that all repository layer references exist in layers list.

    When layers are defined, every repository must reference a defined layer.
    When no layers are defined, layer references are unchecked (any value
    is treated as an ad-hoc layer ID).
    """
    layer_list = data.get("layers", [])
    layer_ids = {layer["id"] for layer in layer_list if isinstance(layer, dict) and "id" in layer}
    if not layer_ids:
        return []
    errors: list[str] = []
    for i, repo in enumerate(data.get("repositories", [])):
        if not isinstance(repo, dict):
            continue
        layer = repo.get("layer", "")
        if layer and layer not in layer_ids:
            errors.append(
                f"repositories[{i}].layer: '{layer}' is not defined in layers"
            )
    return errors
