"""Loads the published Direct API spec and derives the action catalogue."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_SPEC = Path.home() / "IdeaProjects" / "api-docs-portal" / "specs" / "openapi-v1.yml"

METHODS = ("get", "post", "put", "patch", "delete")


class SpecError(Exception):
    pass


def _ref_name(schema):
    """Resolve a schema node to a component name, seeing through arrays."""
    if not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        return _ref_name(schema.get("items", {}))
    return None


class Operation:
    def __init__(self, path, method, raw):
        self.path = path
        self.method = method
        self.operation_id = raw.get("operationId") or "{}{}".format(method, path)
        self.raw = raw

    @property
    def path_params(self):
        return [p["name"] for p in self.raw.get("parameters", []) if p.get("in") == "path"]

    @property
    def request_schema(self):
        content = self.raw.get("requestBody", {}).get("content", {})
        return _ref_name(content.get("application/json", {}).get("schema", {}))

    @property
    def success_schema(self):
        for code, response in self.raw.get("responses", {}).items():
            if not str(code).startswith("2"):
                continue
            content = response.get("content", {})
            name = _ref_name(content.get("application/json", {}).get("schema", {}))
            if name:
                return name
        return None

    def __repr__(self):
        return "<{} {}>".format(self.method.upper(), self.path)


class Spec:
    def __init__(self, document):
        self.document = document
        self.schemas = document.get("components", {}).get("schemas", {})
        self.operations = []
        for path, node in document.get("paths", {}).items():
            for method in METHODS:
                if method in node:
                    self.operations.append(Operation(path, method, node[method]))

    @classmethod
    def load(cls, path=None):
        path = Path(path or os.environ.get("DIRECT_API_SPEC") or DEFAULT_SPEC)
        if not path.exists():
            raise SpecError(
                "Direct API spec not found at {}. It lives in the b7hio/api-docs-portal "
                "repository, not in the exchange repository. Clone it, or point "
                "DIRECT_API_SPEC at the file.".format(path)
            )
        return cls(yaml.safe_load(path.read_text()))

    def direct_operations(self):
        return [op for op in self.operations if op.path.startswith("/direct/")]

    def schema_fields(self, name):
        """Every field a response schema can carry, following one level of $ref."""
        if not name or name not in self.schemas:
            return {}
        fields = {}
        for field, definition in self.schemas[name].get("properties", {}).items():
            fields[field] = definition
            nested = _ref_name(definition)
            if nested and nested != name:
                inner_properties = self.schemas.get(nested, {}).get("properties", {})
                for inner, inner_definition in inner_properties.items():
                    fields.setdefault(inner, inner_definition)
        return fields

    def enum_values(self, schema_name, field):
        definition = self.schema_fields(schema_name).get(field, {})
        referenced = _ref_name(definition)
        if referenced:
            definition = self.schemas.get(referenced, {})
        return definition.get("enum", [])
