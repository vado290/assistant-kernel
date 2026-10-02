import pytest

from assistant_kernel.services.schema_validation import (
    SchemaValidationError,
    validate_json_schema,
)


def test_validates_required_and_additional_properties() -> None:
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    }
    validate_json_schema({"name": "ok"}, schema)
    with pytest.raises(SchemaValidationError):
        validate_json_schema({"name": "ok", "extra": True}, schema)


def test_rejects_unsupported_keyword() -> None:
    with pytest.raises(SchemaValidationError):
        validate_json_schema("x", {"type": "string", "pattern": "x"})
