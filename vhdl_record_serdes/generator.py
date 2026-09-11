"""VHDL code generation: one package + one package body for a set of records.

Packing conventions: the first field declared sits on the least significant
bits, the last field on the most significant ones; inside an array field, the
element with the lowest index sits on the least significant bits.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .model import Field, FieldKind, Naming, RecordDef, VhdlSerdesError, Width

IND = "  "
OFFSET_VAR = "element_low"


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
def element_base(fld: Field, naming: Naming) -> str:
    """Generated base name of the record a field is made of."""
    info = fld.array
    if info is not None:
        if info.element_record:
            return naming.base(info.element_record)
        return naming.base_of_vector_type(info.vector_type)
    return naming.base(fld.record_type or "")


def element_width(fld: Field, naming: Naming) -> Width:
    """Width of one array element."""
    info = fld.array
    if info is None:
        raise VhdlSerdesError(f"le champ '{fld.name}' n'est pas un tableau")
    if info.element_kind is FieldKind.RECORD:
        return Width.symbolic(naming.width_const_of(element_base(fld, naming)))
    return info.element_width


def field_width(fld: Field, naming: Naming) -> Width:
    """Width of one field, nested records expressed through their constant.

    This is what the generated VHDL uses: a nested record contributes
    ``<NESTED>_SERIALIZED_WIDTH`` rather than a hard-coded number, so the
    generated package stays correct if the nested record changes.
    """
    if fld.kind is FieldKind.RECORD:
        return Width.symbolic(naming.width_const(fld.record_type))
    if fld.kind.is_array:
        return element_width(fld, naming).times(fld.array.count)
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
    if fld.kind.is_array:
        info = fld.array
        if not info.count.is_static:
            return None
        if info.element_kind is FieldKind.RECORD:
            bits = sizes.get((info.element_record or "").lower())
        else:
            bits = (info.element_width.const if info.element_width.is_static
                    else None)
        return None if bits is None else bits * info.count.const
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
    if fld.kind.is_single_bit:
        return f"{signal}({low})"
    high = naming.bound_const(rec.name, fld.name, "HIGH")
    return f"{signal}({high} downto {low})"


def _span(low: str, width: Width) -> str:
    """``low + w - 1 downto low``, kept short when the width is static."""
    if width.is_static:
        top = low if width.const == 1 else f"{low} + {width.const - 1}"
    else:
        top = f"{low} + {width.render()} - 1"
    return f"{top} downto {low}"


def _as_slv(expr: str, kind: FieldKind, base_type: str) -> str:
    """Wrap ``expr`` in a conversion to std_logic_vector when needed."""
    if kind.is_single_bit or base_type.lower() == "std_logic_vector":
        return expr
    return f"std_logic_vector({expr})"


def _from_slv_expr(expr: str, kind: FieldKind, base_type: str) -> str:
    if kind.is_single_bit or base_type.lower() == "std_logic_vector":
        return expr
    return f"{base_type}({expr})"


def _to_slv(fld: Field, naming: Naming) -> str:
    """Right-hand side turning ``value.<field>`` into a std_logic_vector."""
    src = f"value.{fld.name}"
    if fld.kind is FieldKind.RECORD:
        return f"{naming.serialize_fn(fld.record_type)}({src})"
    if fld.kind is FieldKind.RECORD_VECTOR:
        base = element_base(fld, naming)
        return f"{naming.serialize_vector_fn_of(base)}({src})"
    return _as_slv(src, fld.kind, fld.base_type)


def _from_slv(fld: Field, rec: RecordDef, naming: Naming) -> str:
    sl = _slice_of(fld, rec, naming, "src")
    if fld.kind is FieldKind.RECORD:
        return f"{naming.deserialize_fn(fld.record_type)}({sl})"
    if fld.kind is FieldKind.RECORD_VECTOR:
        base = element_base(fld, naming)
        return f"{naming.deserialize_vector_fn_of(base)}({sl})"
    return _from_slv_expr(sl, fld.kind, fld.base_type)


def _aligned(pairs: list[tuple[str, str]], sep: str = " := ") -> list[str]:
    width = max((len(lhs) for lhs, _ in pairs), default=0)
    return [f"{lhs.ljust(width)}{sep}{rhs}" for lhs, rhs in pairs]


def _render_statements(items: list, indent: str) -> list[str]:
    """Render assignments (aligned in runs) and multi-line blocks in order."""
    out: list[str] = []
    run: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, tuple):
            run.append(item)
            continue
        if run:
            out += [f"{indent}{line};" for line in _aligned(run)]
            run = []
        out += [f"{indent}{line}" for line in item]
    if run:
        out += [f"{indent}{line};" for line in _aligned(run)]
    return out


def _decl_lines(entries: list[tuple[str, str, str]], indent: str) -> list[str]:
    """Render aligned ``constant`` / ``variable`` declarations."""
    pad = max((len(name) for _, name, _ in entries), default=0)
    return [f"{indent}{kind} {name.ljust(pad)} : {what};"
            for kind, name, what in entries]


def _vector_decl(fld: Field, naming: Naming) -> str:
    """Type of the temporary holding a deserialized array."""
    info = fld.array
    if not info.type_unconstrained:
        return info.vector_type
    count = info.count
    last = str(count.const - 1) if count.is_static else f"{count.render()} - 1"
    return f"{info.vector_type}(0 to {last})"


# --------------------------------------------------------------------------
# per-field statements
# --------------------------------------------------------------------------
def _scalar_array_block(fld: Field, rec: RecordDef, naming: Naming,
                        serialize: bool) -> list[str]:
    """Element-by-element copy of an array of std_logic / slv / unsigned."""
    info = fld.array
    low = naming.bound_const(rec.name, fld.name, "LOW")
    width = info.element_width
    if serialize:
        array_ref = f"value.{fld.name}"
        left = (f"result({OFFSET_VAR})" if info.element_kind.is_single_bit
                else f"result({_span(OFFSET_VAR, width)})")
        right = _as_slv(f"{array_ref}(i)", info.element_kind,
                        info.element_base_type)
    else:
        array_ref = f"result.{fld.name}"
        left = f"{array_ref}(i)"
        chunk = (f"src({OFFSET_VAR})" if info.element_kind.is_single_bit
                 else f"src({_span(OFFSET_VAR, width)})")
        right = _from_slv_expr(chunk, info.element_kind,
                               info.element_base_type)
    # the '* 1' of single-bit elements would be noise
    stride = "" if width.is_static and width.const == 1 else f" * {width.render()}"
    return [
        f"for i in {array_ref}'range loop",
        f"{IND}{OFFSET_VAR} := {low} + (i - {array_ref}'low){stride};",
        f"{IND}{left} := {right};",
        "end loop;",
    ]


def _descending_array_block(fld: Field, rec: RecordDef, naming: Naming,
                            temp: str) -> list[str]:
    """Deserialize a 'downto' record array, keeping index order, not position."""
    base = element_base(fld, naming)
    ref = f"result.{fld.name}"
    return [
        f"{temp} := {naming.deserialize_vector_fn_of(base)}"
        f"({_slice_of(fld, rec, naming, 'src')});",
        f"-- '{fld.name}' runs downto: map by index, lowest index on the LSBs",
        f"for i in {ref}'range loop",
        f"{IND}{ref}(i) := {temp}(i - {ref}'low);",
        "end loop;",
    ]


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
        note = ""
        if fld.kind.is_array:
            note = "  (lowest index on the LSBs)"
        out.append(f"{IND}--   {pos}  {fld.name} : {fld.type_name}{note}")
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
    if rec.vector is not None:
        base = naming.base(rec.name)
        vec = rec.vector.name
        out.append("")
        out.append(f"{IND}-- {vec}: serialized element by element, "
                   f"lowest index on the LSBs")
        out.append(f"{IND}function {naming.serialize_vector_fn_of(base)} "
                   f"(value : {vec}) return std_logic_vector;")
        out.append(f"{IND}function {naming.deserialize_vector_fn_of(base)} "
                   f"(data : std_logic_vector) return {vec};")
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
        if fld.kind.is_single_bit:
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
    items: list = []
    needs_offset = False
    for fld in rec.fields:
        if fld.kind is FieldKind.SCALAR_VECTOR:
            needs_offset = True
            items.append(_scalar_array_block(fld, rec, naming, True))
        else:
            items.append((_slice_of(fld, rec, naming, "result"),
                          _to_slv(fld, naming)))

    decls = [("variable", "result", f"std_logic_vector({width} - 1 downto 0)")]
    if needs_offset:
        decls.append(("variable", OFFSET_VAR, "natural"))
    out = [f"{IND}function {fn} (value : {rec.name}) return std_logic_vector is"]
    out += _decl_lines(decls, IND * 2)
    out.append(f"{IND}begin")
    out += _render_statements(items, IND * 2)
    out += [f"{IND * 2}return result;", f"{IND}end function {fn};", ""]
    return out


def _deserialize_body(rec: RecordDef, naming: Naming,
                      with_assert: bool) -> list[str]:
    fn = naming.deserialize_fn(rec.name)
    width = naming.width_const(rec.name)
    items: list = []
    decls: list[tuple[str, str, str]] = [
        ("variable", "src", f"std_logic_vector({width} - 1 downto 0)"),
        ("variable", "result", rec.name),
    ]
    temps: list[tuple[str, str, str]] = []
    needs_offset = False
    for fld in rec.fields:
        if fld.kind is FieldKind.SCALAR_VECTOR:
            needs_offset = True
            items.append(_scalar_array_block(fld, rec, naming, False))
        elif fld.kind is FieldKind.RECORD_VECTOR and fld.array.descending:
            temp = f"{fld.name}_v"
            temps.append(("variable", temp, _vector_decl(fld, naming)))
            items.append(_descending_array_block(fld, rec, naming, temp))
        else:
            items.append((f"result.{fld.name}", _from_slv(fld, rec, naming)))

    if needs_offset:
        decls.append(("variable", OFFSET_VAR, "natural"))
    out = [f"{IND}function {fn} (data : std_logic_vector) return {rec.name} is"]
    out += _decl_lines(decls + temps, IND * 2)
    out.append(f"{IND}begin")
    if with_assert:
        out += _length_assert(fn, f"data'length = {width}", width)
    out.append(f"{IND * 2}src := data;")
    out += _render_statements(items, IND * 2)
    out += [f"{IND * 2}return result;", f"{IND}end function {fn};", ""]
    return out


def _length_assert(fn: str, condition: str, width: str) -> list[str]:
    return [
        f"{IND * 2}assert {condition}",
        f"{IND * 3}report \"{fn}: expected \" & integer'image({width})",
        f"{IND * 3}       & \" bits, got \" & integer'image(data'length)",
        f"{IND * 3}severity failure;",
    ]


def _vector_bodies(rec: RecordDef, naming: Naming,
                   with_assert: bool) -> list[str]:
    """The two functions handling an array of this record."""
    if rec.vector is None:
        return []
    base = naming.base(rec.name)
    vec = rec.vector.name
    width = naming.width_const(rec.name)
    ser = naming.serialize_vector_fn_of(base)
    deser = naming.deserialize_vector_fn_of(base)
    span = f"{OFFSET_VAR} + {width} - 1 downto {OFFSET_VAR}"

    out = [f"{IND}function {ser} (value : {vec}) return std_logic_vector is"]
    out += _decl_lines(
        [("variable", "result",
          f"std_logic_vector(value'length * {width} - 1 downto 0)"),
         ("variable", OFFSET_VAR, "natural")], IND * 2)
    out += [
        f"{IND}begin",
        f"{IND * 2}for i in value'range loop",
        f"{IND * 3}{OFFSET_VAR} := (i - value'low) * {width};",
        f"{IND * 3}result({span}) := {naming.serialize_fn_of(base)}(value(i));",
        f"{IND * 2}end loop;",
        f"{IND * 2}return result;",
        f"{IND}end function {ser};",
        "",
    ]

    out.append(f"{IND}function {deser} (data : std_logic_vector) return {vec} is")
    decls: list[tuple[str, str, str]] = []
    if rec.vector.unconstrained:
        decls.append(("constant", "COUNT", f"natural := data'length / {width}"))
    decls += [
        ("variable", "src", "std_logic_vector(data'length - 1 downto 0)"),
        ("variable", "result",
         f"{vec}(0 to COUNT - 1)" if rec.vector.unconstrained else vec),
        ("variable", OFFSET_VAR, "natural"),
    ]
    out += _decl_lines(decls, IND * 2)
    out.append(f"{IND}begin")
    if with_assert:
        count = "COUNT" if rec.vector.unconstrained else "result'length"
        out += [
            f"{IND * 2}assert data'length = {count} * {width}",
            f"{IND * 3}report \"{deser}: expected \" "
            f"& integer'image({count} * {width})",
            f"{IND * 3}       & \" bits, got \" & integer'image(data'length)",
            f"{IND * 3}severity failure;",
        ]
    out += [
        f"{IND * 2}src := data;",
        f"{IND * 2}for i in result'range loop",
        f"{IND * 3}{OFFSET_VAR} := (i - result'low) * {width};",
        f"{IND * 3}result(i) := {naming.deserialize_fn_of(base)}(src({span}));",
        f"{IND * 2}end loop;",
        f"{IND * 2}return result;",
        f"{IND}end function {deser};",
        "",
    ]
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
        "-- Automatically generated by vhdl_record_serdes -- DO NOT EDIT.",
        "--",
        "-- Record serialization helpers. Packing convention: the first field of a",
        "-- record occupies the least significant bits, the last field the most",
        "-- significant ones; in an array, the lowest index occupies the least",
        "-- significant bits. A nested record is serialized through the functions",
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
        lines += _vector_bodies(rec, naming, opts.include_assert)
    lines += [f"end package body {opts.package_name};", ""]

    return "\n".join(lines)
