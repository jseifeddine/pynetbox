"""
(c) 2017 DigitalOcean

Client-side payload validation helpers using the NetBox OpenAPI schema.

This module provides a validator that can be used by endpoints prior to
issuing write operations. It validates payloads against the OpenAPI
requestBody schema for the appropriate path+method.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple, Union


try:
    from jsonschema import Draft202012Validator, RefResolver
except Exception:
    Draft202012Validator = None
    RefResolver = None


class ValidationResult:
    """Dot-accessible validation result.

    Always exposes:
    - ok (bool)
    - errors (dict[str, any]) when ok is False
    - reason (str) when ok is False
    """

    def __init__(
        self,
        *,
        ok: bool,
        errors: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.ok: bool = ok
        if not ok:
            self.errors: Dict[str, Any] = errors or {}
            self.reason: str = reason or "validation_failed"

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        if self.ok:
            return "ValidationResult(ok=True)"
        # Prefer structured details for readability if present
        display_errors = getattr(self, "details", None) or self.errors
        return f"ValidationResult(ok=False, errors={display_errors!r}, reason={getattr(self, 'reason', None)!r})"


def _normalize_payload(data: Any) -> Tuple[List[Dict[str, Any]], bool]:
    if isinstance(data, list):
        return data, True
    elif isinstance(data, dict):
        return [data], False
    else:
        raise ValueError(
            f"data must be dict or list[dict] - was {type(data)}"
        )


def _get_request_schema(api, app_name: str, endpoint_name: str, method: str) -> Optional[Dict[str, Any]]:
    """Extract the OpenAPI requestBody schema for the given path+method.

    Returns None if not available.
    """
    try:
        paths = api.openapi()["paths"]
    except Exception:
        return None

    path = f"/api/{app_name}/{endpoint_name}/"
    method = method.lower()
    operation = paths.get(path, {}).get(method)
    if not operation:
        return None

    request_body = operation.get("requestBody", {})
    content = request_body.get("content", {})
    app_json = content.get("application/json", {})
    schema = app_json.get("schema")
    if not schema:
        return None

    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return schema["items"]
    return schema


def _build_validator(openapi_root: Dict[str, Any], item_schema: Dict[str, Any]):
    if Draft202012Validator is None or RefResolver is None:
        return None

    try:
        resolver = RefResolver.from_schema(openapi_root)
    except Exception:
        resolver = None
    try:
        return Draft202012Validator(schema=item_schema, resolver=resolver)
    except Exception:
        return None


def _extract_constraints(schema_fragment: Any) -> Dict[str, Any]:
    """Extract a concise subset of constraints from a schema fragment."""
    if not isinstance(schema_fragment, dict):
        return {}
    keys = [
        "type",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "pattern",
        "format",
        "enum",
    ]
    out: Dict[str, Any] = {}
    for k in keys:
        if k in schema_fragment:
            out[k] = schema_fragment[k]
    if isinstance(out.get("enum"), list) and len(out["enum"]) > 20:
        out["enum"] = out["enum"][:20] + ["…"]
    return out


def _label_from_validator(validator_name: str) -> str:
    mapping = {
        "required": "Missing required",
        "maxLength": "Too long",
        "minLength": "Too short",
        "minimum": "Below minimum",
        "maximum": "Above maximum",
        "type": "Invalid type",
        "enum": "Invalid choice",
        "pattern": "Pattern mismatch",
        "format": "Invalid format",
        "additionalProperties": "Unknown field",
        "anyOf": "Constraint violation",
        "allOf": "Constraint violation",
        "oneOf": "Constraint violation",
    }
    return mapping.get(validator_name, "Validation error")


def validate_payload_against_openapi(
    api,
    app_name: str,
    endpoint_name: str,
    method: str,
    data: Any,
) -> Union[List[ValidationResult], ValidationResult]:
    """Validate payloads for POST and PATCH only.

    Returns list[ValidationResult] when given a list; returns a single
    ValidationResult when given a dict.
    """
    method = method.lower()
    items, is_batch = _normalize_payload(data)

    try:
        openapi_root = api.openapi()
    except Exception:
        return ([ValidationResult(ok=True) for _ in items] if is_batch else ValidationResult(ok=True))

    item_schema = _get_request_schema(api, app_name, endpoint_name, method)
    if not item_schema:
        return ([ValidationResult(ok=True) for _ in items] if is_batch else ValidationResult(ok=True))

    original_required = []
    if isinstance(item_schema, dict):
        original_required = list(item_schema.get("required", []))

    if method == "patch" and isinstance(item_schema, dict):
        item_schema = copy.deepcopy(item_schema)
        if is_batch:
            keep_required = [f for f in original_required if f == "id"]
            if keep_required:
                item_schema["required"] = keep_required
            else:
                item_schema.pop("required", None)
        else:
            item_schema.pop("required", None)

    validator = _build_validator(openapi_root, item_schema)
    if validator is None:
        return ([ValidationResult(ok=True) for _ in items] if is_batch else ValidationResult(ok=True))

    results: List[ValidationResult] = []

    def _skip_required_error_for_patch(err, obj) -> bool:
        if method != "patch":
            return False
        try:
            if getattr(err, "validator", None) != "required":
                return False
            path_parts = list(getattr(err, "path", []))
            if not path_parts:
                missing_field = str(err.message).split("'")[1]
                if is_batch and missing_field == "id" and "id" in original_required:
                    return False
                return missing_field not in obj
            top = path_parts[0]
            return top not in obj
        except Exception:
            return False

    for _, item in enumerate(items):
        field_errors: Dict[str, Dict[str, Any]] = {}
        for error in validator.iter_errors(item):
            if _skip_required_error_for_patch(error, item):
                continue
            path_parts = list(getattr(error, "path", []))
            field = ".".join(str(p) for p in path_parts) if path_parts else "__root__"
            # For required, use the missing field name if extractable
            if (getattr(error, "validator", None) == "required"):
                try:
                    missing_field = str(getattr(error, "message", "")).split("'")[1]
                    if missing_field:
                        field = missing_field
                except Exception:
                    pass
            label = _label_from_validator((getattr(error, "validator", "") or "").strip())
            constraints = _extract_constraints(getattr(error, "schema", {}))
            field_errors[field] = {"error": label, "constraints": constraints}

        if field_errors:
            results.append(
                ValidationResult(
                    ok=False,
                    errors=field_errors,
                    reason="openapi_request_body_validation_failed",
                )
            )
        else:
            results.append(ValidationResult(ok=True))

    return results if is_batch else (results[0] if results else ValidationResult(ok=True))

