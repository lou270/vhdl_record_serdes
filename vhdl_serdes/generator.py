"""VHDL code generation: one package + one package body for a set of records.

Packing convention: the first field declared sits on the least significant bits
(offset 0), the last field on the most significant bits.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .model import Field, FieldKind, Naming, RecordDef, VhdlSerdesError, Width

IND = "  "


@dataclass
class GenOptions:
    package_name: str
    naming: Naming = dc_field(default_factory=Naming)
    library: str = "work"
    use_packages: list[str] = dc_field(default_factory=list)
    extra_use: list[str] = dc_field(default_factory=list)
    include_assert: bool = True
    header_note: str = ""
    sources: list[str] = dc_field(default_factory=list)


# --------------------------------------------------------------------------
# width / layout helpers
# --------------------------------------------------------------------------
def field_width(fld: Field, naming: Naming) -> Width:
    """Width of one field, nested records expressed through their constant.

    This is what the generated VHDL uses: a nested record contributes
    ``<NESTED>_SERIALIZED_WIDTH`` rather than a hard-coded number, so the
    generated package stays correct if the nested record changes.
    """
    if fld.kind is FieldKind.RECORD:
        return Width.symbolic(naming.width_const(fld.record_type))
    return fld.width


def record_width_parts(rec: RecordDef, naming: Naming) -> list[str]:
    return [field_width(f, naming).render() for f in rec.fields]


def record_width(rec: RecordDef, naming: Naming) -> Width:
    total = Width()
    for f in rec.fields:
        total = total + field_width(f, naming)
    return total


def field_bits(fld: Field, sizes: dict[str, int]) -> int | None:
    """Numeric width of a field, or None when only the VHDL tool can know it."""
    if fld.kind is FieldKind.RECORD:
        return sizes.get((fld.record_type or "").lower())
    return fld.width.const if fld.width.is_static else None


def static_sizes(records: list[RecordDef]) -> dict[str, int]:
    """Numeric widths of the records whose size is fully known here.

    Used for the human-readable bit maps only; records with generic-dependent
    bounds or externally defined nested records are simply absent.
    """
    sizes: dict[str, int] = {}
    for rec in sort_records(list(records)):
        total = 0
        for fld in rec.fields:
            bits = field_bits(fld, sizes)
            if bits is None:
                break
            total += bits
        else:
            sizes[rec.name.lower()] = total
    return sizes


def layout(rec: RecordDef, sizes: dict[str, int] | None = None
           ) -> list[tuple[Field, int | None, int | None]]:
    """(field, low, high) with numeric bounds while they stay statically known."""
    sizes = sizes if sizes is not None else {}
    rows: list[tuple[Field, int | None, int | None]] = []
    offset: int | None = 0
    for fld in rec.fields:
        bits = field_bits(fld, sizes)
        if offset is None or bits is None:
            rows.append((fld, offset, None))
            offset = None
        else:
            rows.append((fld, offset, offset + bits - 1))
            offset += bits
    return rows


def sort_records(records: list[RecordDef]) -> list[RecordDef]:
    """Topological sort so a nested record is declared before its user."""
    by_name = {r.name.lower(): r for r in records}
    ordered: list[RecordDef] = []
    state: dict[str, int] = {}

    def visit(rec: RecordDef, stack: tuple[str, ...]) -> None:
        key = rec.name.lower()
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            raise VhdlSerdesError("dependance circulaire entre records: "
                                  + " -> ".join(stack + (rec.name,)))
        state[key] = 1
        for dep in rec.dependencies:
            child = by_name.get(dep.lower())
            if child is not None:
                visit(child, stack + (rec.name,))
        state[key] = 2
        ordered.append(rec)

    for rec in records:
        visit(rec, ())
    return ordered


# --------------------------------------------------------------------------
# expression helpers
# --------------------------------------------------------------------------
def _slice_of(fld: Field, rec: RecordDef, naming: Naming, signal: str) -> str:
    low = naming.bound_const(rec.name, fld.name, "LOW")
    if fld.kind is FieldKind.STD_LOGIC:
        return f"{signal}({low})"
    high = naming.bound_const(rec.name, fld.name, "HIGH")
    return f"{signal}({high} downto {low})"


def _to_slv(fld: Field, naming: Naming) -> str:
    """Right-hand side turning ``value.<field>`` into a std_logic_vector."""
    src = f"value.{fld.name}"
    if fld.kind is FieldKind.RECORD:
        return f"{naming.serialize_fn(fld.record_type)}({src})"
    if fld.kind is FieldKind.STD_LOGIC:
        return src
    if fld.base_type.lower() == "std_logic_vector":
        return src
    return f"std_logic_vector({src})"


def _from_slv(fld: Field, rec: RecordDef, naming: Naming) -> str:
    sl = _slice_of(fld, rec, naming, "src")
    if fld.kind is FieldKind.RECORD:
        return f"{naming.deserialize_fn(fld.record_type)}({sl})"
    if fld.kind is FieldKind.STD_LOGIC:
        return sl
    if fld.base_type.lower() == "std_logic_vector":
        return sl
    return f"{fld.base_type}({sl})"


def _aligned(pairs: list[tuple[str, str]], sep: str = " := ") -> list[str]:
    width = max((len(lhs) for lhs, _ in pairs), default=0)
    return [f"{lhs.ljust(width)}{sep}{rhs}" for lhs, rhs in pairs]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _banner(text: str, indent: str = IND) -> list[str]:
    rule = "-" * (79 - len(indent))
    return [f"{indent}{rule}", f"{indent}-- {text}", f"{indent}{rule}"]


def _layout_comment(rec: RecordDef, naming: Naming,
                    sizes: dict[str, int]) -> list[str]:
    out = [f"{IND}-- Bit map, first field on the LSBs:"]
    for fld, low, high in layout(rec, sizes):
        if low is not None and high is not None:
            pos = f"[{high:>4} : {low:>4}]"
        else:
            pos = f"[+{field_width(fld, naming).render():>11}]"
        out.append(f"{IND}--   {pos}  {fld.name} : {fld.type_name}")
    bits = sizes.get(rec.name.lower())
    if bits is not None:
        out.append(f"{IND}--   total: {bits} bits")
    return out


def _declaration(rec: RecordDef, naming: Naming,
                 sizes: dict[str, int]) -> list[str]:
    out: list[str] = []
    out += _banner(f"{rec.name}  ({rec.source}:{rec.line})")
    out += _layout_comment(rec, naming, sizes)
    parts = " + ".join(record_width_parts(rec, naming))
    bits = sizes.get(rec.name.lower())
    suffix = f"  -- {bits} bits" if bits is not None and len(rec.fields) > 1 else ""
    out.append(f"{IND}constant {naming.width_const(rec.name)} : natural "
               f":= {parts};{suffix}")
    out.append("")
    out.append(f"{IND}function {naming.serialize_fn(rec.name)} "
               f"(value : {rec.name}) return std_logic_vector;")
    out.append(f"{IND}function {naming.deserialize_fn(rec.name)} "
               f"(data : std_logic_vector) return {rec.name};")
    out.append("")
    return out


def _bound_constants(rec: RecordDef, naming: Naming) -> list[str]:
    """Private bit offsets, each one chained to the end of the previous field."""
    rows: list[tuple[str, str]] = []
    previous: str | None = None
    for fld in rec.fields:
        low = naming.bound_const(rec.name, fld.name, "LOW")
        rows.append((low, "0" if previous is None else f"{previous} + 1"))
        previous = low
        if fld.kind is FieldKind.STD_LOGIC:
            continue  # single bit: the HIGH bound would be redundant
        width = field_width(fld, naming)
        if width.is_static:
            expr = low if width.const == 1 else f"{low} + {width.const - 1}"
        else:
            expr = f"{low} + {width.render()} - 1"
        high = naming.bound_const(rec.name, fld.name, "HIGH")
        rows.append((high, expr))
        previous = high
    pad = max(len(name) for name, _ in rows)
    return [f"{IND}constant {name.ljust(pad)} : natural := {expr};"
            for name, expr in rows]


def _serialize_body(rec: RecordDef, naming: Naming) -> list[str]:
    fn = naming.serialize_fn(rec.name)
    width = naming.width_const(rec.name)
    out = [
        f"{IND}function {fn} (value : {rec.name}) return std_logic_vector is",
        f"{IND * 2}variable result : std_logic_vector({width} - 1 downto 0);",
        f"{IND}begin",
    ]
    pairs = [(_slice_of(f, rec, naming, "result"), _to_slv(f, naming))
             for f in rec.fields]
    out += [f"{IND * 2}{line};" for line in _aligned(pairs)]
    out += [f"{IND * 2}return result;", f"{IND}end function {fn};", ""]
    return out


def _deserialize_body(rec: RecordDef, naming: Naming,
                      with_assert: bool) -> list[str]:
    fn = naming.deserialize_fn(rec.name)
    width = naming.width_const(rec.name)
    out = [
        f"{IND}function {fn} (data : std_logic_vector) return {rec.name} is",
        f"{IND * 2}variable src    : std_logic_vector({width} - 1 downto 0);",
        f"{IND * 2}variable result : {rec.name};",
        f"{IND}begin",
    ]
    if with_assert:
        out += [
            f"{IND * 2}assert data'length = {width}",
            f"{IND * 3}report \"{fn}: expected \" & integer'image({width})",
            f"{IND * 3}       & \" bits, got \" & integer'image(data'length)",
            f"{IND * 3}severity failure;",
        ]
    out.append(f"{IND * 2}src := data;")
    pairs = [(f"result.{f.name}", _from_slv(f, rec, naming)) for f in rec.fields]
    out += [f"{IND * 2}{line};" for line in _aligned(pairs)]
    out += [f"{IND * 2}return result;", f"{IND}end function {fn};", ""]
    return out


def _check_base_names(records: list[RecordDef], naming: Naming) -> None:
    """Two record types must not reduce to the same generated identifier."""
    seen: dict[str, str] = {}
    types = [rec.name for rec in records]
    types += [f.record_type for rec in records for f in rec.fields
              if f.record_type]
    for type_name in types:
        base = naming.base(type_name).lower()
        previous = seen.setdefault(base, type_name)
        if previous.lower() != type_name.lower():
            raise VhdlSerdesError(
                f"'{type_name}' et '{previous}' donnent le meme nom de base "
                f"'{naming.base(type_name)}' : les identifiants generes "
                f"entreraient en collision (ajustez --strip-type-suffix)")


def generate(records: list[RecordDef], opts: GenOptions) -> str:
    if not records:
        raise VhdlSerdesError("aucun record a generer")
    ordered = sort_records(records)
    sizes = static_sizes(ordered)
    naming = opts.naming
    _check_base_names(ordered, naming)

    rule = "-" * 80
    lines: list[str] = [
        rule,
        "-- Automatically generated by vhdl_serdes -- DO NOT EDIT.",
        "--",
        "-- Record serialization helpers. Packing convention: the first field of a",
        "-- record occupies the least significant bits, the last field the most",
        "-- significant ones. A nested record is serialized through the functions",
        "-- of the same naming convention.",
    ]
    if opts.sources:
        lines += ["--", "-- Source(s):"] + [f"--   {s}" for s in opts.sources]
    if opts.header_note:
        lines += ["--", f"-- {opts.header_note}"]
    lines += [rule, ""]

    lines += [
        "library ieee;",
        f"{IND}use ieee.std_logic_1164.all;",
        f"{IND}use ieee.numeric_std.all;",
        "",
    ]
    for pkg in opts.use_packages:
        lines.append(f"use {opts.library}.{pkg}.all;")
    for clause in opts.extra_use:
        lines.append(clause if clause.rstrip().endswith(";") else clause + ";")
    if opts.use_packages or opts.extra_use:
        lines.append("")

    lines += [f"package {opts.package_name} is", ""]
    for rec in ordered:
        lines += _declaration(rec, naming, sizes)
    lines += [f"end package {opts.package_name};", ""]

    lines += [f"package body {opts.package_name} is", ""]
    for rec in ordered:
        lines += _banner(rec.name)
        lines += _bound_constants(rec, naming)
        lines.append("")
        lines += _serialize_body(rec, naming)
        lines += _deserialize_body(rec, naming, opts.include_assert)
    lines += [f"end package body {opts.package_name};", ""]

    return "\n".join(lines)
