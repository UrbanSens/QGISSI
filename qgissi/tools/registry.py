"""Declarative tool registry shared by the plugin, the benchmark and the MCP server.

A tool is a plain Python function decorated with :func:`tool`. The decorator
records a JSON schema for the model, a risk level for the approval policy, a
category for the UI and a tier (``core`` tools are always offered; ``extended``
tools are offered when the user enables the full tool set, which helps small
local models that cope badly with long tool lists).
"""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.cancel import CancelledError, CancelToken
from ..providers.base import Block, ToolSpec, image_block, text_block


class Risk:
    READ = "read"                # inspects the project or data; changes nothing
    NETWORK = "network"          # sends a query to an external service
    WRITE = "write"              # adds layers / changes styles in the project (undoable)
    DESTRUCTIVE = "destructive"  # edits or deletes data, removes layers, writes files
    CODE = "code"                # runs model-written Python

    ORDER = ("read", "network", "write", "destructive", "code")


class ToolError(Exception):
    """An expected failure; the message goes back to the model verbatim."""


@dataclass
class ToolResult:
    text: str = ""
    data: Any = None
    images: List[Tuple[str, str]] = field(default_factory=list)  # (media_type, base64)
    is_error: bool = False
    layers_created: List[str] = field(default_factory=list)

    def render_text(self, max_chars: int = 12000) -> str:
        text = self.text
        if not text and self.data is not None:
            text = json.dumps(self.data, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(text) > max_chars:
            cut = len(text) - max_chars
            text = text[:max_chars] + f"\n... [truncated {cut} characters; request a smaller subset]"
        return text or "(done)"

    def to_blocks(self, max_chars: int = 12000) -> List[Block]:
        blocks: List[Block] = [text_block(self.render_text(max_chars))]
        for media_type, data in self.images:
            blocks.append(image_block(data, media_type))
        return blocks


@dataclass
class ToolContext:
    project: Any
    iface: Any = None
    output_dir: str = ""
    cancel: CancelToken = field(default_factory=CancelToken)
    run_id: str = ""
    headless: bool = True
    checkpoint: Any = None
    ask_user: Optional[Callable[[str, List[str]], str]] = None
    progress: Optional[Callable[[str], None]] = None
    allow_network: bool = True
    canvas_size: Tuple[int, int] = (1024, 768)
    state: Dict[str, Any] = field(default_factory=dict)

    def report(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


@dataclass
class ToolDef:
    name: str
    description: str
    func: Callable[..., Any]
    parameters: Dict[str, Any] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)
    risk: str = Risk.READ
    category: str = "general"
    tier: str = "core"
    requires_gui: bool = False
    untrusted: bool = False      # output may contain third-party text (prompt-injection surface)

    def annotations(self) -> Dict[str, Any]:
        """MCP-style behaviour hints derived from the risk level."""
        return {"readOnlyHint": self.risk == Risk.READ,
                "destructiveHint": self.risk in (Risk.DESTRUCTIVE, Risk.CODE),
                "openWorldHint": self.risk == Risk.NETWORK or self.untrusted}

    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": self.parameters, "required": list(self.required),
                "additionalProperties": False}

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema())


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolDef] = {}

    def add(self, definition: ToolDef) -> ToolDef:
        if definition.name in self._tools:
            raise ValueError(f"Duplicate tool name: {definition.name}")
        self._tools[definition.name] = definition
        return definition

    def tool(self, name: str, description: str, parameters: Optional[Dict[str, Any]] = None,
             required: Optional[List[str]] = None, risk: str = Risk.READ, category: str = "general",
             tier: str = "core", requires_gui: bool = False, untrusted: bool = False):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add(ToolDef(name=name, description=" ".join(description.split()), func=func,
                             parameters=parameters or {}, required=required or [], risk=risk,
                             category=category, tier=tier, requires_gui=requires_gui, untrusted=untrusted))
            return func
        return decorator

    def get(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def select(self, tier: str = "full", gui: bool = True,
               exclude: Optional[List[str]] = None) -> List[ToolDef]:
        chosen = []
        for definition in self._tools.values():
            if tier == "core" and definition.tier != "core":
                continue
            if definition.requires_gui and not gui:
                continue
            if exclude and definition.name in exclude:
                continue
            chosen.append(definition)
        return sorted(chosen, key=lambda d: d.name)

    # ------------------------------------------------------------ validation
    @staticmethod
    def _coerce(value: Any, schema: Dict[str, Any]) -> Tuple[Any, Optional[str]]:
        expected = schema.get("type")
        if isinstance(expected, list):
            expected = next((t for t in expected if t != "null"), None)
            if value is None:
                return value, None
        try:
            if expected == "integer" and not isinstance(value, bool):
                if isinstance(value, str):
                    value = float(value.strip())
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                if not isinstance(value, int):
                    return value, "must be an integer"
            elif expected == "number" and not isinstance(value, bool):
                if isinstance(value, str):
                    value = float(value.strip())
                if not isinstance(value, (int, float)):
                    return value, "must be a number"
            elif expected == "boolean":
                if isinstance(value, str) and value.lower() in ("true", "false", "yes", "no", "1", "0"):
                    value = value.lower() in ("true", "yes", "1")
                elif isinstance(value, (int, float)) and value in (0, 1):
                    value = bool(value)
                if not isinstance(value, bool):
                    return value, "must be true or false"
            elif expected == "string":
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = str(value)
                if not isinstance(value, str):
                    return value, "must be a string"
            elif expected == "array":
                if isinstance(value, str):
                    stripped = value.strip()
                    value = json.loads(stripped) if stripped.startswith("[") else [value]
                if not isinstance(value, list):
                    value = [value]
                item_schema = schema.get("items")
                if isinstance(item_schema, dict):
                    coerced = []
                    for item in value:
                        item, err = ToolRegistry._coerce(item, item_schema)
                        if err:
                            return value, f"items {err}"
                        coerced.append(item)
                    value = coerced
            elif expected == "object":
                if isinstance(value, str):
                    value = json.loads(value)
                if not isinstance(value, dict):
                    return value, "must be an object"
        except (ValueError, TypeError):
            return value, f"must be of type {expected}"
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            match = next((e for e in enum if isinstance(e, str) and isinstance(value, str)
                          and e.lower() == value.lower()), None)
            if match is None:
                return value, f"must be one of {enum}"
            value = match
        return value, None

    def validate(self, definition: ToolDef, args: Any) -> Tuple[Dict[str, Any], List[str]]:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return {}, ["arguments must be a JSON object"]
        clean: Dict[str, Any] = {}
        errors: List[str] = []
        for key, value in args.items():
            schema = definition.parameters.get(key)
            if schema is None:
                continue  # ignore unknown keys some models add
            if value is None and key not in definition.required:
                continue
            value, err = self._coerce(value, schema)
            if err:
                errors.append(f"'{key}' {err}")
            else:
                clean[key] = value
        for key in definition.required:
            if key not in clean and not any(e.startswith(f"'{key}'") for e in errors):
                errors.append(f"missing required argument '{key}'")
        return clean, errors

    def invoke(self, definition: ToolDef, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        clean, errors = self.validate(definition, args)
        if errors:
            props = ", ".join(f"{k} ({v.get('type', 'any')})" for k, v in definition.parameters.items())
            return ToolResult(text=f"Invalid arguments for {definition.name}: {'; '.join(errors)}. "
                                   f"Expected parameters: {props or 'none'}.", is_error=True)
        try:
            value = definition.func(ctx, **clean)
        except CancelledError:
            raise
        except ToolError as exc:
            return ToolResult(text=str(exc), is_error=True)
        except Exception as exc:  # report the failing line so the model can adapt
            frames = traceback.extract_tb(exc.__traceback__)
            where = f" (at {frames[-1].name}:{frames[-1].lineno})" if frames else ""
            return ToolResult(text=f"{type(exc).__name__}: {exc}{where}", is_error=True)
        return as_result(value)


def as_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult(text=value)
    return ToolResult(data=value)


REGISTRY = ToolRegistry()
tool = REGISTRY.tool
