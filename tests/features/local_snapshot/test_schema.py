"""JSON Schema and pydantic agreement tests for the local snapshot format.

``src/sideboard/schemas/local-snapshot.schema.json`` and the pydantic models in
``features/local_snapshot/models.py`` are two views of one contract. These tests compare
them structurally (required keys, declared types, extra-key policy, string bounds, list
bounds, enum values) and check that the fixture set validates or fails consistently.

A tiny structural checker stands in for a JSON Schema validator so the suite needs no
extra dependency. The one intentional asymmetry is documented in its test: JSON Schema
cannot express "no control characters", so the model is strictly stronger there.
"""

from __future__ import annotations

import json
import re
import types as types_module
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic.fields import FieldInfo

from sideboard.core.contracts import Privacy
from sideboard.core.errors import ParseError
from sideboard.features.local_snapshot.models import (
    MAX_HISTORY_POINTS,
    MAX_ITEMS,
    MAX_METRICS,
    SCHEMA_VERSION,
    SnapshotDocument,
    SnapshotItem,
    SnapshotMetric,
)
from sideboard.features.local_snapshot.validate import (
    SNAPSHOT_SCHEMA,
    SNAPSHOT_SCHEMA_PATH,
    load_snapshot_schema,
    validate_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"

PYTHON_FOR_SCHEMA: dict[str, tuple[type, ...]] = {
    "string": (str, datetime),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
    "null": (type(None),),
}


@pytest.fixture(scope="module")
def schema() -> Mapping[str, Any]:
    decoded = json.loads(SNAPSHOT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def valid_document() -> dict[str, Any]:
    decoded = json.loads((FIXTURES / "valid_snapshot.json").read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return decoded


# --------------------------------------------------------------------------- #
# Structural mini-checker (the subset of JSON Schema the format uses)
# --------------------------------------------------------------------------- #


def _type_names(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {str(name) for name in declared}
    return set()


def _allows(types: tuple[type, ...], target: type) -> bool:
    return any(issubclass(target, allowed) for allowed in types)


def _accepts_scalar(value: Any, schema: Mapping[str, Any]) -> bool:
    if "const" in schema:
        return bool(value == schema["const"])
    names = _type_names(schema)
    allowed: tuple[type, ...] = ()
    for name in names:
        allowed += PYTHON_FOR_SCHEMA.get(name, ())
    if not allowed:
        return not names
    if isinstance(value, str):
        if not _allows(allowed, str):
            return False
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            return False
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            return False
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            return False
        min_length = schema.get("minLength")
        return not (isinstance(min_length, int) and len(value) < min_length)
    if value is None:
        return _allows(allowed, type(None))
    return _allows(allowed, type(value))


def _violations(value: Any, schema: Mapping[str, Any], path: str = "document") -> list[str]:
    problems: list[str] = []
    if isinstance(value, Mapping):
        if "object" not in _type_names(schema):
            return [f"{path}: expected a JSON object"]
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"{path}.{key}: required key is missing")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    problems.append(f"{path}.{key}: unknown key")
        for key, sub in properties.items():
            if key in value and isinstance(sub, Mapping):
                problems.extend(_violations(value[key], sub, f"{path}.{key}"))
        return problems
    if isinstance(value, list):
        if "array" not in _type_names(schema):
            return [f"{path}: expected an array"]
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            problems.append(f"{path}: more than {max_items} entries")
        sub = schema.get("items")
        if isinstance(sub, Mapping):
            for index, entry in enumerate(value):
                problems.extend(_violations(entry, sub, f"{path}[{index}]"))
        return problems
    if not _accepts_scalar(value, schema):
        problems.append(f"{path}: {value!r} does not satisfy {sorted(_type_names(schema))}")
    return problems


def _branch(schema_property: Mapping[str, Any]) -> Mapping[str, Any]:
    """Pick the non-null branch of an ``anyOf`` property for comparison."""
    if "anyOf" in schema_property:
        for candidate in schema_property["anyOf"]:
            if isinstance(candidate, Mapping) and candidate.get("type") != "null":
                return candidate
    return schema_property


def _constraint(field: FieldInfo, name: str) -> Any:
    for meta in field.metadata:
        value = getattr(meta, name, None)
        if value is not None:
            return value
    return None


def _annotation_targets(annotation: Any) -> list[Any]:
    """Unwrap ``X | None`` and generic containers into the classes an annotation holds."""
    origin = get_origin(annotation)
    if origin in (Union, types_module.UnionType):
        targets: list[Any] = []
        for argument in get_args(annotation):
            targets.extend(_annotation_targets(argument))
        return targets
    if origin in (tuple, list, set, frozenset):
        return [tuple]
    return [annotation]


def _annotation_fits(annotation: Any, names: set[str]) -> bool:
    allowed: tuple[type, ...] = ()
    for name in names:
        allowed += PYTHON_FOR_SCHEMA.get(name, ())
    if not allowed:
        return True
    for target in _annotation_targets(annotation):
        if target is Any:
            continue
        if not isinstance(target, type) or not _allows(allowed, target):
            return False
    return True


def _assert_object_agreement(
    object_schema: Mapping[str, Any], model: type[Any], label: str
) -> None:
    required = set(object_schema.get("required") or [])
    properties = object_schema.get("properties") or {}
    fields = model.model_fields
    assert set(properties) == set(fields), f"{label}: property names differ"
    for name, field in fields.items():
        assert (name in required) == field.is_required(), f"{label}.{name}: required mismatch"
    assert object_schema.get("additionalProperties") is False, label
    assert model.model_config.get("extra") == "forbid", label


# --------------------------------------------------------------------------- #
# The file itself
# --------------------------------------------------------------------------- #


def test_schema_file_is_valid_json_with_a_draft_id(schema: Mapping[str, Any]) -> None:
    assert SNAPSHOT_SCHEMA_PATH.name == "local-snapshot.schema.json"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert isinstance(schema.get("$id"), str) and schema["$id"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["schema_version", "source_id", "generated_at"]
    assert isinstance(schema.get("title"), str) and schema["title"]
    assert isinstance(schema.get("description"), str) and "1 MiB" in schema["description"]


def test_public_schema_mapping_is_lazy_cached_and_matches_the_file(
    schema: Mapping[str, Any],
) -> None:
    assert dict(SNAPSHOT_SCHEMA) == schema
    assert load_snapshot_schema() is load_snapshot_schema()


def test_required_keys_and_field_names_agree_with_the_model(schema: Mapping[str, Any]) -> None:
    properties = dict(schema)["properties"]
    _assert_object_agreement(schema, SnapshotDocument, "document")
    metric_entry = dict(dict(properties["metrics"])["items"])
    item_entry = dict(dict(properties["items"])["items"])
    _assert_object_agreement(metric_entry, SnapshotMetric, "metric")
    _assert_object_agreement(item_entry, SnapshotItem, "item")


def test_declared_types_agree_with_the_model(schema: Mapping[str, Any]) -> None:
    properties = dict(schema)["properties"]
    document_fields = SnapshotDocument.model_fields
    for name, raw in properties.items():
        assert isinstance(raw, Mapping)
        assert _annotation_fits(document_fields[name].annotation, _type_names(raw)), name

    metric_properties = dict(dict(properties["metrics"])["items"])["properties"]
    metric_fields = SnapshotMetric.model_fields
    for name, raw in metric_properties.items():
        assert isinstance(raw, Mapping)
        assert _annotation_fits(metric_fields[name].annotation, _type_names(raw)), name

    item_properties = dict(dict(properties["items"])["items"])["properties"]
    item_fields = SnapshotItem.model_fields
    for name, raw in item_properties.items():
        assert isinstance(raw, Mapping)
        assert _annotation_fits(item_fields[name].annotation, _type_names(raw)), name


def test_string_bounds_and_patterns_agree_with_the_model(schema: Mapping[str, Any]) -> None:
    properties = dict(schema)["properties"]
    document_fields = SnapshotDocument.model_fields
    for name in ("source_id", "title"):
        branch = _branch(dict(properties[name]))
        assert branch.get("maxLength") == _constraint(document_fields[name], "max_length")
        assert branch.get("pattern") == _constraint(document_fields[name], "pattern")
        assert branch.get("minLength") == _constraint(document_fields[name], "min_length")

    metric_properties = dict(dict(properties["metrics"])["items"])["properties"]
    metric_fields = SnapshotMetric.model_fields
    for name in ("id", "label", "unit", "basis"):
        branch = _branch(dict(metric_properties[name]))
        assert branch.get("maxLength") == _constraint(metric_fields[name], "max_length"), name
        assert branch.get("pattern") == _constraint(metric_fields[name], "pattern"), name

    item_properties = dict(dict(properties["items"])["items"])["properties"]
    item_fields = SnapshotItem.model_fields
    for name in ("id", "title", "subtitle", "url"):
        branch = _branch(dict(item_properties[name]))
        assert branch.get("maxLength") == _constraint(item_fields[name], "max_length"), name


def test_bounds_and_versions_agree_with_the_model_constants(schema: Mapping[str, Any]) -> None:
    properties = dict(schema)["properties"]
    assert properties["schema_version"]["const"] == SCHEMA_VERSION
    assert dict(properties["metrics"])["maxItems"] == MAX_METRICS
    assert dict(properties["items"])["maxItems"] == MAX_ITEMS
    metric_properties = dict(dict(properties["metrics"])["items"])["properties"]
    assert dict(metric_properties["history"])["maxItems"] == MAX_HISTORY_POINTS
    assert set(properties["privacy"]["enum"]) == {str(member) for member in Privacy}
    assert properties["privacy"]["default"] == "private"


def test_date_time_formats_are_declared(schema: Mapping[str, Any]) -> None:
    properties = dict(schema)["properties"]
    assert properties["generated_at"]["format"] == "date-time"
    assert dict(_branch(dict(properties["expires_at"])))["format"] == "date-time"
    item_properties = dict(dict(properties["items"])["items"])["properties"]
    assert dict(_branch(dict(item_properties["published_at"])))["format"] == "date-time"


# --------------------------------------------------------------------------- #
# Fixture consistency
# --------------------------------------------------------------------------- #


def test_valid_fixture_passes_both_views(schema: Mapping[str, Any]) -> None:
    document = valid_document()
    assert _violations(document, schema) == []
    payload = validate_snapshot(document)
    assert payload.document.source_id == "example-backup"
    assert payload.metrics


def test_invalid_fixture_fails_both_views(schema: Mapping[str, Any]) -> None:
    document = json.loads((FIXTURES / "invalid_snapshot.json").read_text(encoding="utf-8"))
    problems = _violations(document, schema)
    assert any("generated_at" in problem for problem in problems)
    assert any("url" in problem for problem in problems)
    with pytest.raises(ParseError) as excinfo:
        validate_snapshot(document)
    assert "generated_at" in excinfo.value.message
    assert "url" in excinfo.value.message


def test_missing_required_key_is_rejected_by_both_views(schema: Mapping[str, Any]) -> None:
    document = valid_document()
    del document["source_id"]
    problems = _violations(document, schema)
    assert any("source_id" in problem for problem in problems)
    with pytest.raises(ParseError) as excinfo:
        validate_snapshot(document)
    assert "source_id" in excinfo.value.message


def test_unknown_top_level_key_is_rejected_by_both_views(schema: Mapping[str, Any]) -> None:
    document = valid_document()
    document["unexpected"] = True
    problems = _violations(document, schema)
    assert any("unexpected" in problem for problem in problems)
    with pytest.raises(ParseError):
        validate_snapshot(document)


def test_partial_fixture_is_not_even_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads((FIXTURES / "partial_snapshot.json").read_text(encoding="utf-8"))


def test_model_is_stricter_than_the_schema_for_control_characters(
    schema: Mapping[str, Any],
) -> None:
    """Documented asymmetry: the model refuses control characters, JSON Schema cannot."""
    document = valid_document()
    document["metrics"] = [{"id": "note", "label": "Note", "value": "line\nbreak"}]
    assert _violations(document, schema) == []
    with pytest.raises(ParseError):
        validate_snapshot(document)


def test_oversized_fixture_is_generated_at_test_time() -> None:
    """The oversized case is built in ``tmp_path``, not committed as a megabyte file."""
    assert not (FIXTURES / "oversized_snapshot.json").exists()
