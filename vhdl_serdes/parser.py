"""A small, dependency-free VHDL parser: just enough to read record types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .model import Field, FieldKind, RecordDef, VhdlSerdesError, Width, eval_static

_SL_TYPES = {"std_logic", "std_ulogic"}
_SLV_TYPES = {"std_logic_vector", "std_ulogic_vector"}
_UNSIGNED_TYPES = {"unsigned", "u_unsigned"}
_SIGNED_TYPES = {"signed", "u_signed"}

# Types we deliberately refuse rather than guessing an encoding for.
_UNSUPPORTED_SCALARS = {
    "integer": "utilisez unsigned/signed avec une largeur explicite",
    "natural": "utilisez unsigned avec une largeur explicite",
    "positive": "utilisez unsigned avec une largeur explicite",
    "boolean": "utilisez std_logic",
    "real": "pas de representation binaire portable",
    "time": "pas de representation binaire portable",
    "character": "non supporte",
    "string": "non supporte",
    "bit": "utilisez std_logic",
    "bit_vector": "utilisez std_logic_vector",
}

_RE_PACKAGE = re.compile(r"\bpackage\s+(?!body\b)([A-Za-z]\w*)\s+is\b", re.I)
_RE_RECORD = re.compile(r"\btype\s+([A-Za-z]\w*)\s+is\s+record\b", re.I)
_RE_END_RECORD = re.compile(r"\bend\s+record\b", re.I)
_RE_ARRAY = re.compile(r"\btype\s+([A-Za-z]\w*)\s+is\s+array\b", re.I)
_RE_ENUM = re.compile(r"\btype\s+([A-Za-z]\w*)\s+is\s*\(", re.I)
_RE_SUBTYPE = re.compile(r"\bsubtype\s+([A-Za-z]\w*)\s+is\s+([^;]+);", re.I)

_QUOTE = '"'
_TICK = "'"


def strip_comments(text: str) -> str:
    """Blank out comments while keeping every character offset and line intact."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == _QUOTE:  # string literal
            i += 1
            while i < n and text[i] != _QUOTE:
                i += 1
            i += 1
        elif c == _TICK and i + 2 < n and text[i + 2] == _TICK:  # char literal
            i += 3
        elif c == "-" and text.startswith("--", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
        else:
            i += 1
    return "".join(out)


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split on ``sep`` outside of any parenthesis."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _find_top_level(text: str, pattern: str) -> int:
    """Offset of ``pattern`` (regex, outside parentheses), or -1."""
    depth = 0
    for m in re.finditer(r"\(|\)|" + pattern, text, re.I):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            return m.start()
    return -1


@dataclass
class _TypeDecl:
    kind: str          # record | array | enum | subtype
    name: str
    payload: str = ""  # subtype: the type mark it aliases
    source: str = ""
    line: int = 0


class VhdlSource:
    """The declarations collected from one or more VHDL files."""

    def __init__(self) -> None:
        self.records: dict[str, RecordDef] = {}   # keyed by lowercase name
        self.decls: dict[str, _TypeDecl] = {}     # keyed by lowercase name
        self.packages: list[str] = []
        self.order: list[str] = []                # record names, in source order
        self.warnings: list[str] = []

    # -- parsing ---------------------------------------------------------
    def add_file(self, path: str | Path) -> None:
        p = Path(path)
        if not p.is_file():
            raise VhdlSerdesError(f"fichier introuvable: {p}")
        try:
            raw = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = p.read_text(encoding="latin-1")
        self.add_text(raw, source=str(p))

    def add_text(self, raw: str, source: str = "<text>") -> None:
        text = strip_comments(raw)

        packages = [(m.start(), m.group(1)) for m in _RE_PACKAGE.finditer(text)]
        for _, name in packages:
            if name not in self.packages:
                self.packages.append(name)

        for regex, kind in ((_RE_ARRAY, "array"), (_RE_ENUM, "enum")):
            for m in regex.finditer(text):
                self._add_decl(_TypeDecl(kind, m.group(1), source=source,
                                         line=_line_of(text, m.start())))
        for m in _RE_SUBTYPE.finditer(text):
            self._add_decl(_TypeDecl("subtype", m.group(1), payload=m.group(2),
                                     source=source,
                                     line=_line_of(text, m.start())))

        for m in _RE_RECORD.finditer(text):
            name = m.group(1)
            end = _RE_END_RECORD.search(text, m.end())
            if end is None:
                raise VhdlSerdesError(
                    f"{source}: 'end record' manquant pour le type '{name}'")
            line = _line_of(text, m.start())
            pkg = None
            for off, pname in packages:
                if off < m.start():
                    pkg = pname
            rec = RecordDef(name=name, package=pkg, source=source, line=line)
            self._add_decl(_TypeDecl("record", name, source=source, line=line))
            key = name.lower()
            if key in self.records:
                prev = self.records[key]
                raise VhdlSerdesError(
                    f"{source}:{line}: le record '{name}' est deja defini dans "
                    f"{prev.source}:{prev.line}")
            self.records[key] = rec
            self.order.append(name)
            rec.fields = self._parse_body(rec, text[m.end():end.start()], line)

    def _add_decl(self, decl: _TypeDecl) -> None:
        self.decls.setdefault(decl.name.lower(), decl)

    def _parse_body(self, rec: RecordDef, body: str, base_line: int) -> list[Field]:
        fields: list[Field] = []
        seen: set[str] = set()
        for chunk in _split_top_level(body, ";"):
            if not chunk.strip():
                continue
            colon = _find_top_level(chunk, ":")
            if colon < 0:
                raise VhdlSerdesError(
                    f"{rec.source}:{base_line}: element de record illisible "
                    f"dans '{rec.name}': '{chunk.strip()}'")
            names = [n.strip() for n in chunk[:colon].split(",") if n.strip()]
            type_text = chunk[colon + 1:].strip()
            if not names:
                raise VhdlSerdesError(
                    f"{rec.source}:{base_line}: element sans nom dans "
                    f"'{rec.name}': '{chunk.strip()}'")
            for name in names:
                if not re.fullmatch(r"[A-Za-z]\w*", name):
                    raise VhdlSerdesError(
                        f"{rec.source}:{base_line}: nom de champ invalide "
                        f"'{name}' dans '{rec.name}'")
                if name.lower() in seen:
                    raise VhdlSerdesError(
                        f"{rec.source}:{base_line}: champ '{name}' duplique "
                        f"dans '{rec.name}'")
                seen.add(name.lower())
                fields.append(self._make_field(rec, name, type_text, base_line))
        if not fields:
            raise VhdlSerdesError(
                f"{rec.source}:{base_line}: le record '{rec.name}' est vide")
        return fields

    # -- type resolution -------------------------------------------------
    def _make_field(self, rec: RecordDef, name: str, type_text: str,
                    line: int) -> Field:
        where = f"{rec.source}:{line}: {rec.name}.{name}"
        kind, base, width, record_type, external = self._resolve(type_text, where)
        return Field(name=name, kind=kind, type_name=" ".join(type_text.split()),
                     width=width, base_type=base, record_type=record_type,
                     external=external, line=line)

    def _resolve(self, type_text: str, where: str, _seen: tuple[str, ...] = ()
                 ) -> tuple[FieldKind, str, Width, str | None, bool]:
        text = " ".join(type_text.split())
        m = re.fullmatch(r"([A-Za-z]\w*(?:\s*\.\s*[A-Za-z]\w*)*)\s*(?:\((.*)\))?",
                         text, re.S)
        if not m:
            raise VhdlSerdesError(f"{where}: type illisible '{type_text}'")
        full_name, range_text = m.group(1), m.group(2)
        simple = full_name.split(".")[-1].strip().lower()

        if simple in _SL_TYPES:
            if range_text:
                raise VhdlSerdesError(
                    f"{where}: '{simple}' ne prend pas d'intervalle")
            return FieldKind.STD_LOGIC, simple, Width.static(1), None, False

        for names, kind in (
            (_SLV_TYPES, FieldKind.STD_LOGIC_VECTOR),
            (_UNSIGNED_TYPES, FieldKind.UNSIGNED),
            (_SIGNED_TYPES, FieldKind.SIGNED),
        ):
            if simple in names:
                if not range_text:
                    raise VhdlSerdesError(
                        f"{where}: '{simple}' non contraint, precisez un "
                        f"intervalle (ex: {simple}(7 downto 0))")
                return kind, simple, _range_width(range_text, where), None, False

        if simple in _UNSUPPORTED_SCALARS:
            raise VhdlSerdesError(
                f"{where}: type '{simple}' non supporte "
                f"({_UNSUPPORTED_SCALARS[simple]})")

        decl = self.decls.get(simple)
        if decl is not None and decl.kind == "record":
            if range_text:
                raise VhdlSerdesError(
                    f"{where}: un record ne prend pas d'intervalle")
            return FieldKind.RECORD, decl.name, Width(), decl.name, False
        if decl is not None and decl.kind in ("array", "enum"):
            what = "tableau" if decl.kind == "array" else "type enumere"
            raise VhdlSerdesError(
                f"{where}: '{decl.name}' est un {what} "
                f"({decl.source}:{decl.line}), non supporte")
        if decl is not None and decl.kind == "subtype":
            if simple in _seen:
                raise VhdlSerdesError(
                    f"{where}: resolution circulaire du subtype '{decl.name}'")
            kind, mark, width, rec_type, ext = self._resolve(
                decl.payload, where, _seen + (simple,))
            if range_text:  # subtype of an unconstrained type, constrained here
                width = _range_width(range_text, where)
            return kind, mark, width, rec_type, ext

        # Unknown type mark: assume a record declared elsewhere that follows the
        # same naming convention (documented behaviour).
        if range_text:
            raise VhdlSerdesError(
                f"{where}: type inconnu '{full_name}' avec un intervalle; "
                f"un record externe ne prend pas d'intervalle")
        self.warnings.append(
            f"{where}: type '{full_name}' inconnu, suppose etre un record "
            f"fournissant les fonctions de la meme convention de nommage")
        return FieldKind.RECORD, full_name, Width(), full_name, True


def _range_width(range_text: str, where: str) -> Width:
    text = " ".join(range_text.split())
    down = _find_top_level(text, r"\bdownto\b")
    if down >= 0:
        high, low = text[:down].strip(), text[down + len("downto"):].strip()
    else:
        to = _find_top_level(text, r"\bto\b")
        if to < 0:
            raise VhdlSerdesError(
                f"{where}: intervalle illisible '({range_text})', attendu "
                f"'<high> downto <low>' ou '<low> to <high>'")
        low, high = text[:to].strip(), text[to + len("to"):].strip()
    if not high or not low:
        raise VhdlSerdesError(f"{where}: intervalle incomplet '({range_text})'")
    hi_val, lo_val = eval_static(high), eval_static(low)
    if hi_val is not None and lo_val is not None:
        width = hi_val - lo_val + 1
        if width <= 0:
            raise VhdlSerdesError(f"{where}: intervalle vide '({range_text})'")
        return Width.static(width)
    if lo_val == 0:
        return Width.symbolic(f"(({high}) + 1)")
    if lo_val is not None:
        sign = "-" if lo_val > 1 else "+"
        return Width.symbolic(f"(({high}) {sign} {abs(lo_val - 1)})")
    if hi_val is not None:
        return Width.symbolic(f"({hi_val + 1} - ({low}))")
    return Width.symbolic(f"(({high}) - ({low}) + 1)")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def parse_files(paths: list) -> VhdlSource:
    src = VhdlSource()
    for p in paths:
        src.add_file(p)
    return src
