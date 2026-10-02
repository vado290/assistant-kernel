from __future__ import annotations

from typing import Any


class SchemaValidationError(ValueError):
    pass


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the JSON-Schema subset used by Kernel workflow contracts.

    Supported keywords are deliberately explicit: type, required, properties,
    additionalProperties, items, enum, const, min/max length, and min/max items.
    Unknown keywords are rejected so a workflow cannot appear more strictly
    validated than it really is.
    """
    supported = {
        "$schema", "$id", "title", "description", "type", "required",
        "properties", "additionalProperties", "items", "enum", "const",
        "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
    }
    unknown = set(schema) - supported
    if unknown:
        raise SchemaValidationError(f"{path}: unsupported schema keywords: {sorted(unknown)}")
    expected = schema.get("type")
    if expected is not None:
        matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        if expected not in matches:
            raise SchemaValidationError(f"{path}: unsupported type {expected!r}")
        if not matches[expected]:
            raise SchemaValidationError(f"{path}: expected {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: value is not in enum")
    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path}: value does not match const")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise SchemaValidationError(f"{path}: missing required properties {missing}")
        additional = schema.get("additionalProperties", True)
        if additional is False:
            extras = set(value) - set(properties)
            if extras:
                raise SchemaValidationError(f"{path}: additional properties {sorted(extras)}")
        for name, child in properties.items():
            if name in value:
                validate_json_schema(value[name], child, f"{path}.{name}")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise SchemaValidationError(f"{path}: too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise SchemaValidationError(f"{path}: too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_json_schema(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise SchemaValidationError(f"{path}: string is too short")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise SchemaValidationError(f"{path}: string is too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError(f"{path}: above maximum")
