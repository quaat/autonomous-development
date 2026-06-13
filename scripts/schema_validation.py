"""Structured-payload validation against the bundled JSON Schemas.

The structured boundaries the controller currently validates here against a
bundled Draft 2020-12 schema — *before* the payload is published to a canonical
artifact or merged into run state — are the Codex enhance/plan/review outputs,
the reconciliation source and decision delta, and the triage ledger.
(Persisted run-state validation is not wired through this module yet; it is
deferred until the run-state schema is finalized.)

Design invariants:

* **Fail closed.** A schema that cannot be loaded, is itself invalid, or that
  the payload violates raises ``SchemaValidationError``. There is no silent
  downgrade to "assume valid": if we cannot certify a payload we reject it.
* **Deterministic, actionable errors.** Validation errors are sorted by their
  location and rendered with a JSON Pointer so the failing field is obvious and
  the message is stable across runs (important for tests and audit logs).
* **Cached validators.** Compiled validators are cached by schema path so
  repeated validation in a single process does not re-read or re-compile the
  schema.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = PLUGIN_ROOT / "schemas"

try:  # jsonschema is a declared runtime dependency; import failure is fail-closed.
    import jsonschema
    from jsonschema import Draft202012Validator
    from jsonschema.validators import validator_for

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only without the dep
    jsonschema = None  # type: ignore[assignment]
    Draft202012Validator = None  # type: ignore[assignment]
    validator_for = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


class SchemaValidationError(Exception):
    """Raised when a payload fails validation or a schema cannot be loaded."""


@functools.lru_cache(maxsize=None)
def _load_validator(schema_rel: str):
    """Load, validate, and compile the schema at ``schema_rel`` (cached).

    ``schema_rel`` is a plugin-root-relative path such as
    ``"schemas/review.schema.json"``.
    """
    if jsonschema is None:  # pragma: no cover - dependency-present in CI
        raise SchemaValidationError(
            "jsonschema is required for structured validation but could not be "
            f"imported: {_IMPORT_ERROR}. Install it (it is a declared runtime "
            "dependency) before running the workflow."
        )
    path = PLUGIN_ROOT / schema_rel
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(
            f"Cannot load schema {schema_rel}: {exc}"
        ) from exc
    cls = validator_for(schema, default=Draft202012Validator)
    try:
        cls.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:  # type: ignore[union-attr]
        raise SchemaValidationError(
            f"Bundled schema {schema_rel} is not a valid JSON Schema: {exc.message}"
        ) from exc
    return cls(schema)


def _escape_token(token: Any) -> str:
    """Escape a single JSON Pointer reference token per RFC 6901.

    ``~`` becomes ``~0`` and ``/`` becomes ``~1`` (and ``~`` must be escaped
    first, or a literal ``/`` would be double-encoded). Without this, a property
    name containing ``/`` or ``~`` would render an ambiguous pointer.
    """
    return str(token).replace("~", "~0").replace("/", "~1")


def _pointer(path: Any) -> str:
    parts = list(path)
    if not parts:
        return "(root)"
    return "/" + "/".join(_escape_token(part) for part in parts)


def validate_payload(payload: Any, schema_rel: str, *, label: str | None = None) -> None:
    """Validate ``payload`` against the bundled schema, raising on any violation.

    All violations are reported together (sorted by location) so a caller sees
    every problem at once rather than fixing them one at a time.
    """
    validator = _load_validator(schema_rel)
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda err: (list(err.absolute_path), err.message),
    )
    if not errors:
        return
    what = label or schema_rel
    lines = [f"{what} failed schema validation against {schema_rel}:"]
    for err in errors:
        lines.append(f"  at {_pointer(err.absolute_path)}: {err.message}")
    raise SchemaValidationError("\n".join(lines))
