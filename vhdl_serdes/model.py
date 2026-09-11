"""Data model shared by the VHDL parser and the code generator."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field as dc_field
from enum import Enum


class VhdlSerdesError(Exception):
    """Raised for any user-facing error (bad input, unsupported type, ...)."""


def eval_static(text: str) -> int | None:
    """Evaluate a purely numeric VHDL integer expression, else return None.

    Handles the common bound expressions (``7``, ``8*4-1``, ``(2+3)``).
    Anything referencing a constant or generic stays symbolic.
    """
    try:
        node = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        return None
    allowed = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
        ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Div, ast.Mod,
        ast.UAdd, ast.USub,
    )
    for sub in ast.walk(node):
        if not isinstance(sub, allowed):
            return None
        if isinstance(sub, ast.Constant) and not isinstance(sub.value, int):
            return None
    try:
        value = eval(compile(node, "<bound>", "eval"), {"__builtins__": {}}, {})
    except Exception:
        return None
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class Width:
    """A bit width that may be partly symbolic.

    ``const`` is the statically known part; ``terms`` holds VHDL expressions
    that only the VHDL tool can evaluate (generic-dependent bounds, widths of
    externally defined records, ...).
    """

    const: int = 0
    terms: tuple[str, ...] = ()

    @property
    def is_static(self) -> bool:
        return not self.terms

    def __add__(self, other: "Width") -> "Width":
        return Width(self.const + other.const, self.terms + other.terms)

    def render(self) -> str:
        if not self.terms:
            return str(self.const)
        parts = list(self.terms)
        if self.const:
            parts.append(str(self.const))
        return " + ".join(parts)

    @classmethod
    def static(cls, value: int) -> "Width":
        return cls(const=value)

    @classmethod
    def symbolic(cls, expr: str) -> "Width":
        return cls(terms=(expr,))


class FieldKind(Enum):
    STD_LOGIC = "std_logic"
    STD_LOGIC_VECTOR = "std_logic_vector"
    UNSIGNED = "unsigned"
    SIGNED = "signed"
    RECORD = "record"

    @property
    def is_vector(self) -> bool:
        return self is not FieldKind.STD_LOGIC


@dataclass
class Field:
    """One element of a record."""

    name: str
    kind: FieldKind
    type_name: str                  # type mark as written in the source
    width: Width
    base_type: str = ""             # resolved base type mark (for casts)
    record_type: str | None = None  # nested record type name
    external: bool = False          # nested record not present in the inputs
    line: int = 0


@dataclass
class RecordDef:
    """A ``type <name> is record ... end record;`` declaration."""

    name: str
    fields: list[Field] = dc_field(default_factory=list)
    package: str | None = None
    source: str = ""
    line: int = 0

    @property
    def dependencies(self) -> list[str]:
        """Nested record types declared in the same input set."""
        return [f.record_type for f in self.fields
                if f.record_type and not f.external]


#: Type-name suffixes dropped by default to build the generated identifiers:
#: ``frame_t`` and ``frame_type`` both yield ``frame``.
DEFAULT_TYPE_SUFFIXES = ("_type", "_t")


@dataclass(frozen=True)
class Naming:
    """Naming convention used for the generated identifiers.

    The same rules are applied to nested records, so that a record defined in
    another file resolves to the function names it is expected to provide.
    """

    strip_prefixes: tuple[str, ...] = ()
    strip_suffixes: tuple[str, ...] = DEFAULT_TYPE_SUFFIXES
    width_suffix: str = "SERIALIZED_WIDTH"
    ser_suffix: str = "record2slv"
    deser_suffix: str = "slv2record"

    def base(self, type_name: str) -> str:
        """Name kept from a record type: only the head, without its type tag.

        The longest matching affix wins, and an affix is never allowed to eat
        the whole name (``t_t`` keeps ``t_t`` rather than becoming empty).
        """
        name = type_name
        for prefix in sorted(self.strip_prefixes, key=len, reverse=True):
            if (prefix and len(name) > len(prefix)
                    and name.lower().startswith(prefix.lower())):
                name = name[len(prefix):]
                break
        for suffix in sorted(self.strip_suffixes, key=len, reverse=True):
            if (suffix and len(name) > len(suffix)
                    and name.lower().endswith(suffix.lower())):
                name = name[: -len(suffix)]
                break
        return name

    def width_const(self, type_name: str) -> str:
        return f"{self.base(type_name).upper()}_{self.width_suffix}"

    def serialize_fn(self, type_name: str) -> str:
        return f"{self.base(type_name)}_{self.ser_suffix}"

    def deserialize_fn(self, type_name: str) -> str:
        return f"{self.base(type_name)}_{self.deser_suffix}"

    def bound_const(self, type_name: str, field: str, bound: str) -> str:
        return f"{self.base(type_name).upper()}_{field.upper()}_{bound}"
