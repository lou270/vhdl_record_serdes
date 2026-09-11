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

    def times(self, count: "Width") -> "Width":
        """``count`` elements of this width, kept numeric when both are static."""
        if self.is_static and count.is_static:
            return Width.static(self.const * count.const)
        return Width.symbolic(f"({count.render()} * {self.render()})")

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
    RECORD_VECTOR = "record_vector"    # array of records
    SCALAR_VECTOR = "scalar_vector"    # array of std_logic / slv / unsigned / signed

    @property
    def is_single_bit(self) -> bool:
        return self is FieldKind.STD_LOGIC

    @property
    def is_array(self) -> bool:
        return self in (FieldKind.RECORD_VECTOR, FieldKind.SCALAR_VECTOR)


@dataclass
class ArrayInfo:
    """An array-typed record element.

    Elements are packed lowest index first: the element with the smallest index
    occupies the least significant bits, whichever way the range runs.
    """

    vector_type: str                  # array type mark, as written in the record
    element_kind: FieldKind
    element_base_type: str = ""
    element_width: Width = dc_field(default_factory=Width)
    element_record: str | None = None  # element record type, when known
    count: Width = dc_field(default_factory=Width)
    descending: bool = False           # the field range runs 'downto'
    type_unconstrained: bool = True    # the array TYPE is 'array (... range <>)'


@dataclass
class Field:
    """One element of a record."""

    name: str
    kind: FieldKind
    type_name: str                  # type mark as written in the source
    width: Width = dc_field(default_factory=Width)
    base_type: str = ""             # resolved base type mark (for casts)
    record_type: str | None = None  # record type, directly or as array element
    external: bool = False          # that record is not present in the inputs
    line: int = 0
    array: ArrayInfo | None = None  # set for the array kinds


@dataclass
class VectorType:
    """The array type declared for a record, following the naming convention."""

    name: str
    unconstrained: bool = True
    source: str = ""
    line: int = 0


@dataclass
class RecordDef:
    """A ``type <name> is record ... end record;`` declaration."""

    name: str
    fields: list[Field] = dc_field(default_factory=list)
    package: str | None = None
    source: str = ""
    line: int = 0
    vector: VectorType | None = None  # array type of this record, if any

    @property
    def dependencies(self) -> list[str]:
        """Record types used by this record and declared in the same inputs."""
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
    vector_suffix: str = "vector"
    ser_vector_suffix: str = "recordvector2slv"
    deser_vector_suffix: str = "slv2recordvector"

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

    # -- identifiers built from a record type name -------------------------
    def width_const(self, type_name: str) -> str:
        return self.width_const_of(self.base(type_name))

    def serialize_fn(self, type_name: str) -> str:
        return self.serialize_fn_of(self.base(type_name))

    def deserialize_fn(self, type_name: str) -> str:
        return self.deserialize_fn_of(self.base(type_name))

    def vector_type(self, type_name: str) -> str:
        """Array type expected for a record: ``frame_t`` -> ``frame_vector``."""
        return f"{self.base(type_name)}_{self.vector_suffix}"

    def bound_const(self, type_name: str, field: str, bound: str) -> str:
        return f"{self.base(type_name).upper()}_{field.upper()}_{bound}"

    # -- identifiers built from an already stripped base name --------------
    def width_const_of(self, base: str) -> str:
        return f"{base.upper()}_{self.width_suffix}"

    def serialize_fn_of(self, base: str) -> str:
        return f"{base}_{self.ser_suffix}"

    def deserialize_fn_of(self, base: str) -> str:
        return f"{base}_{self.deser_suffix}"

    def serialize_vector_fn_of(self, base: str) -> str:
        return f"{base}_{self.ser_vector_suffix}"

    def deserialize_vector_fn_of(self, base: str) -> str:
        return f"{base}_{self.deser_vector_suffix}"

    def base_of_vector_type(self, vector_type_name: str) -> str:
        """``frame_vector`` -> ``frame``; any other name is returned as is."""
        tail = f"_{self.vector_suffix}"
        if (len(vector_type_name) > len(tail)
                and vector_type_name.lower().endswith(tail.lower())):
            return vector_type_name[: -len(tail)]
        return vector_type_name

    def is_vector_type_name(self, type_name: str) -> bool:
        tail = f"_{self.vector_suffix}"
        return (len(type_name) > len(tail)
                and type_name.lower().endswith(tail.lower()))
