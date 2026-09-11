"""A small, dependency-free VHDL parser: just enough to read record types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from .model import (ArrayInfo, Field, FieldKind, Naming, RecordDef, VectorType,
                    VhdlSerdesError, Width, eval_static)

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
_RE_ARRAY_HEAD = re.compile(r"\btype\s+([A-Za-z]\w*)\s+is\s+array\s*\(", re.I)
_RE_ARRAY_TAIL = re.compile(r"\s*of\s+([^;]+);", re.I | re.S)
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


def _matching_paren(text: str, open_at: int) -> int:
    """Offset of the ``)`` closing the ``(`` at ``open_at``, or -1."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


@dataclass
class _TypeDecl:
    kind: str                    # record | array | enum | subtype
    name: str
    payload: str = ""            # subtype: aliased type mark; array: element type
    index_text: str = ""         # array: index constraint as written
    unconstrained: bool = False  # array: declared 'array (<type> range <>)'
    multi_dim: bool = False
    source: str = ""
    line: int = 0


@dataclass
class _Resolved:
    """Outcome of resolving one type mark."""

    kind: FieldKind
    base_type: str
    width: Width = dc_field(default_factory=Width)
    record_type: str | None = None
    external: bool = False
    array: ArrayInfo | None = None


@dataclass
class _RecordBody:
    """A record declaration whose body is not resolved yet."""

    name: str
    body: str
    package: str | None
    source: str
    line: int
    discovered: bool = False   # found by scanning a directory, not listed


class VhdlSource:
    """The declarations collected from one or more VHDL files.

    Reading happens in two passes: ``add_file`` / ``add_text`` collect the type
    declarations, then ``finalize`` resolves the record bodies. That way a
    record may use a record declared in a file read later, which matters as
    soon as the files come from scanning a directory.
    """

    def __init__(self, naming: Naming | None = None) -> None:
        self.naming = naming or Naming()
        self.records: dict[str, RecordDef] = {}   # keyed by lowercase name
        self.decls: dict[str, _TypeDecl] = {}     # keyed by lowercase name
        self.packages: list[str] = []
        self.order: list[str] = []                # record names, in source order
        self.warnings: list[str] = []
        self.skipped: dict[str, str] = {}         # record name -> reason
        self._bodies: dict[str, _RecordBody] = {}
        self._finalized = False

    # -- parsing ---------------------------------------------------------
    def add_file(self, path: str | Path, discovered: bool = False) -> None:
        p = Path(path)
        if not p.is_file():
            raise VhdlSerdesError(f"fichier introuvable: {p}")
        try:
            raw = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = p.read_text(encoding="latin-1")
        self.add_text(raw, source=str(p), discovered=discovered)

    def add_text(self, raw: str, source: str = "<text>",
                 discovered: bool = False) -> None:
        if self._finalized:
            raise VhdlSerdesError(
                "VhdlSource.finalize() a deja ete appele, plus rien ne peut "
                "etre ajoute")
        text = strip_comments(raw)

        packages = [(m.start(), m.group(1)) for m in _RE_PACKAGE.finditer(text)]
        for _, name in packages:
            if name not in self.packages:
                self.packages.append(name)

        for m in _RE_ENUM.finditer(text):
            self._add_decl(_TypeDecl("enum", m.group(1), source=source,
                                     line=_line_of(text, m.start())))
        for m in _RE_SUBTYPE.finditer(text):
            self._add_decl(_TypeDecl("subtype", m.group(1), payload=m.group(2),
                                     source=source,
                                     line=_line_of(text, m.start())))
        self._parse_arrays(text, source)

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
            key = name.lower()
            previous = self._bodies.get(key)
            if previous is not None:
                message = (f"{source}:{line}: le record '{name}' est deja "
                           f"defini dans {previous.source}:{previous.line}")
                if not discovered:
                    raise VhdlSerdesError(message)
                self.warnings.append(f"{message} ; la seconde est ignoree")
                continue
            self._add_decl(_TypeDecl("record", name, source=source, line=line))
            self._bodies[key] = _RecordBody(
                name=name, body=text[m.end():end.start()], package=pkg,
                source=source, line=line, discovered=discovered)

    def finalize(self) -> None:
        """Resolve every record body once all the files have been read.

        A record that cannot be resolved is reported as an error when its file
        was named on the command line, and skipped with a warning when the file
        was merely discovered while scanning a directory.
        """
        if self._finalized:
            return
        self._finalized = True

        for key, body in self._bodies.items():
            rec = RecordDef(name=body.name, package=body.package,
                            source=body.source, line=body.line)
            try:
                rec.fields = self._parse_body(rec, body.body, body.line)
            except VhdlSerdesError as exc:
                if not body.discovered:
                    raise
                self._skip(key, body.name, str(exc))
                continue
            self.records[key] = rec
            self.order.append(rec.name)

        self._link_vector_types()
        self._cascade_skips()

    def _skip(self, key: str, name: str, reason: str) -> None:
        self.skipped[key] = reason
        self.warnings.append(f"record '{name}' ignore: {reason}")

    def _cascade_skips(self) -> None:
        """Drop the records that depend on a record which was skipped."""
        changed = True
        while changed:
            changed = False
            for key in list(self.records):
                rec = self.records[key]
                for dep in rec.dependencies:
                    if dep.lower() not in self.skipped:
                        continue
                    reason = (f"{rec.source}:{rec.line}: contient le record "
                              f"'{dep}' qui a ete ignore")
                    if not self._bodies[key].discovered:
                        raise VhdlSerdesError(
                            f"{reason} ({self.skipped[dep.lower()]})")
                    del self.records[key]
                    self.order.remove(rec.name)
                    self._skip(key, rec.name, reason)
                    changed = True
                    break

    def _parse_arrays(self, text: str, source: str) -> None:
        for m in _RE_ARRAY_HEAD.finditer(text):
            name = m.group(1)
            line = _line_of(text, m.start())
            open_at = m.end() - 1
            close = _matching_paren(text, open_at)
            if close < 0:
                raise VhdlSerdesError(
                    f"{source}:{line}: parenthese non fermee dans la "
                    f"declaration du tableau '{name}'")
            index_text = text[open_at + 1:close]
            tail = _RE_ARRAY_TAIL.match(text, close + 1)
            if tail is None:
                raise VhdlSerdesError(
                    f"{source}:{line}: declaration du tableau '{name}' "
                    f"illisible, attendu '... ) of <type>;'")
            self._add_decl(_TypeDecl(
                "array", name,
                payload=tail.group(1).strip(),
                index_text=index_text.strip(),
                unconstrained="<>" in index_text,
                multi_dim=len(_split_top_level(index_text, ",")) > 1,
                source=source, line=line))

    def _add_decl(self, decl: _TypeDecl) -> None:
        self.decls.setdefault(decl.name.lower(), decl)

    def _link_vector_types(self) -> None:
        """Attach to each record the array type declared over it, if any."""
        preferred = {rec.name.lower(): self.naming.vector_type(rec.name).lower()
                     for rec in self.records.values()}
        for decl in self.decls.values():
            if decl.kind != "array" or decl.multi_dim:
                continue
            kept = len(self.warnings)
            try:
                element = self._resolve(decl.payload,
                                        f"{decl.source}:{decl.line}")
            except VhdlSerdesError:
                continue
            finally:
                del self.warnings[kept:]  # no field uses it yet: stay quiet
            if element.kind is not FieldKind.RECORD or not element.record_type:
                continue
            rec = self.records.get(element.record_type.lower())
            if rec is None:
                continue
            wanted = preferred.get(rec.name.lower())
            if rec.vector is None or (decl.name.lower() == wanted
                                      and rec.vector.name.lower() != wanted):
                rec.vector = VectorType(decl.name, decl.unconstrained,
                                        decl.source, decl.line)

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
        res = self._resolve(type_text, where)
        return Field(name=name, kind=res.kind,
                     type_name=" ".join(type_text.split()),
                     width=res.width, base_type=res.base_type,
                     record_type=res.record_type, external=res.external,
                     line=line, array=res.array)

    def _resolve(self, type_text: str, where: str,
                 _seen: tuple[str, ...] = ()) -> _Resolved:
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
            return _Resolved(FieldKind.STD_LOGIC, simple, Width.static(1))

        for names, kind in ((_SLV_TYPES, FieldKind.STD_LOGIC_VECTOR),
                            (_UNSIGNED_TYPES, FieldKind.UNSIGNED),
                            (_SIGNED_TYPES, FieldKind.SIGNED)):
            if simple in names:
                if not range_text:
                    raise VhdlSerdesError(
                        f"{where}: '{simple}' non contraint, precisez un "
                        f"intervalle (ex: {simple}(7 downto 0))")
                width, _ = _range_width(range_text, where)
                return _Resolved(kind, simple, width)

        if simple in _UNSUPPORTED_SCALARS:
            raise VhdlSerdesError(
                f"{where}: type '{simple}' non supporte "
                f"({_UNSUPPORTED_SCALARS[simple]})")

        decl = self.decls.get(simple)
        if decl is not None and decl.kind == "record":
            if range_text:
                raise VhdlSerdesError(
                    f"{where}: un record ne prend pas d'intervalle directement ; "
                    f"pour un tableau de records, declarez 'type "
                    f"{self.naming.vector_type(decl.name)} is array (natural "
                    f"range <>) of {decl.name};'")
            return _Resolved(FieldKind.RECORD, decl.name, Width(),
                             record_type=decl.name)
        if decl is not None and decl.kind == "enum":
            raise VhdlSerdesError(
                f"{where}: '{decl.name}' est un type enumere "
                f"({decl.source}:{decl.line}), non supporte")
        if decl is not None and decl.kind == "array":
            return self._resolve_array(decl, range_text, where, _seen)
        if decl is not None and decl.kind == "subtype":
            if simple in _seen:
                raise VhdlSerdesError(
                    f"{where}: resolution circulaire du subtype '{decl.name}'")
            res = self._resolve(decl.payload, where, _seen + (simple,))
            if range_text and not res.kind.is_array:
                # subtype of an unconstrained vector type, constrained here
                width, _ = _range_width(range_text, where)
                res = _Resolved(res.kind, res.base_type, width,
                                res.record_type, res.external)
            return res

        # Unknown type mark following the vector convention: a record array
        # declared elsewhere.
        if range_text and self.naming.is_vector_type_name(simple):
            count, descending = _range_width(range_text, where)
            base = self.naming.base_of_vector_type(full_name.split(".")[-1])
            self.warnings.append(
                f"{where}: type '{full_name}' inconnu, suppose etre un tableau "
                f"de records '{base}' fournissant les fonctions de la meme "
                f"convention de nommage")
            return _Resolved(
                FieldKind.RECORD_VECTOR, full_name, Width(), external=True,
                array=ArrayInfo(vector_type=full_name,
                                element_kind=FieldKind.RECORD,
                                count=count, descending=descending))

        # Unknown type mark: assume a record declared elsewhere that follows the
        # same naming convention (documented behaviour).
        if range_text:
            raise VhdlSerdesError(
                f"{where}: type inconnu '{full_name}' avec un intervalle ; un "
                f"record externe ne prend pas d'intervalle, et un tableau "
                f"externe doit suivre la convention "
                f"'<nom>_{self.naming.vector_suffix}'")
        self.warnings.append(
            f"{where}: type '{full_name}' inconnu, suppose etre un record "
            f"fournissant les fonctions de la meme convention de nommage")
        return _Resolved(FieldKind.RECORD, full_name, Width(),
                         record_type=full_name, external=True)

    def _resolve_array(self, decl: _TypeDecl, range_text: str | None,
                       where: str, _seen: tuple[str, ...]) -> _Resolved:
        if decl.multi_dim:
            raise VhdlSerdesError(
                f"{where}: '{decl.name}' est un tableau multidimensionnel "
                f"({decl.source}:{decl.line}), non supporte")
        if range_text and not decl.unconstrained:
            raise VhdlSerdesError(
                f"{where}: le type tableau '{decl.name}' est deja contraint "
                f"({decl.source}:{decl.line}), retirez l'intervalle")
        if not range_text and decl.unconstrained:
            raise VhdlSerdesError(
                f"{where}: '{decl.name}' non contraint, precisez un intervalle "
                f"(ex: {decl.name}(0 to 3))")
        count, descending = _range_width(
            range_text if range_text else _index_range(decl.index_text), where)

        if decl.name.lower() in _seen:
            raise VhdlSerdesError(
                f"{where}: resolution circulaire du tableau '{decl.name}'")
        element = self._resolve(decl.payload, where, _seen + (decl.name.lower(),))
        if element.kind.is_array:
            raise VhdlSerdesError(
                f"{where}: '{decl.name}' est un tableau de tableaux "
                f"({decl.source}:{decl.line}), non supporte")

        if element.kind is FieldKind.RECORD:
            kind = FieldKind.RECORD_VECTOR
            if element.record_type:
                expected = self.naming.vector_type(element.record_type)
                if decl.name.lower() != expected.lower():
                    self.warnings.append(
                        f"{decl.source}:{decl.line}: le tableau de "
                        f"'{element.record_type}' est nomme '{decl.name}' et "
                        f"non '{expected}' comme l'attend la convention ; les "
                        f"fonctions generees prennent bien '{decl.name}'")
        else:
            kind = FieldKind.SCALAR_VECTOR

        return _Resolved(
            kind, element.base_type, Width(),
            record_type=element.record_type, external=element.external,
            array=ArrayInfo(vector_type=decl.name,
                            element_kind=element.kind,
                            element_base_type=element.base_type,
                            element_width=element.width,
                            element_record=element.record_type,
                            count=count, descending=descending,
                            type_unconstrained=decl.unconstrained))


def _index_range(index_text: str) -> str:
    """``natural range 0 to 3`` -> ``0 to 3``."""
    pos = _find_top_level(index_text, r"\brange\b")
    return index_text[pos + len("range"):] if pos >= 0 else index_text


def _range_width(range_text: str, where: str) -> tuple[Width, bool]:
    """Number of values in a VHDL range, and whether it runs 'downto'."""
    text = " ".join(range_text.split())
    descending = False
    down = _find_top_level(text, r"\bdownto\b")
    if down >= 0:
        descending = True
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
        count = hi_val - lo_val + 1
        if count <= 0:
            raise VhdlSerdesError(f"{where}: intervalle vide '({range_text})'")
        return Width.static(count), descending
    if lo_val == 0:
        return Width.symbolic(f"(({high}) + 1)"), descending
    if lo_val is not None:
        sign = "-" if lo_val > 1 else "+"
        return Width.symbolic(f"(({high}) {sign} {abs(lo_val - 1)})"), descending
    if hi_val is not None:
        return Width.symbolic(f"({hi_val + 1} - ({low}))"), descending
    return Width.symbolic(f"(({high}) - ({low}) + 1)"), descending


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


#: Extensions looked for when an input path is a directory.
DEFAULT_EXTENSIONS = (".vhd", ".vhdl")


def collect_vhdl_files(paths: list, extensions: tuple = DEFAULT_EXTENSIONS,
                       recursive: bool = True, exclude: list | None = None
                       ) -> list[tuple[Path, bool]]:
    """Expand the inputs into ``(file, discovered)`` pairs, without duplicates.

    A path naming a file is taken as is; a path naming a directory is scanned
    for ``extensions`` and the files found are flagged as discovered, which
    makes the parser tolerant about the ones it cannot handle.
    """
    suffixes = tuple(e.lower() if e.startswith(".") else f".{e.lower()}"
                     for e in extensions)
    skip = {Path(p).resolve() for p in (exclude or [])}
    found: dict[Path, bool] = {}

    for raw in paths:
        path = Path(raw)
        if path.is_file():
            found.setdefault(path.resolve(), False)
            continue
        if not path.is_dir():
            raise VhdlSerdesError(f"fichier ou dossier introuvable: {path}")
        pattern = "**/*" if recursive else "*"
        matches = sorted(p for p in path.glob(pattern)
                         if p.is_file() and p.suffix.lower() in suffixes)
        if not matches:
            raise VhdlSerdesError(
                f"aucun fichier {'/'.join(suffixes)} dans {path}"
                + ("" if recursive else " (sans --no-recursive ?)"))
        for match in matches:
            resolved = match.resolve()
            if resolved not in skip:
                found.setdefault(resolved, True)

    if not found:
        raise VhdlSerdesError("aucun fichier a lire")
    return list(found.items())


def parse_files(paths: list, naming: Naming | None = None,
                **kwargs) -> VhdlSource:
    """Read every input (file or directory) and resolve the records."""
    src = VhdlSource(naming)
    for path, discovered in collect_vhdl_files(paths, **kwargs):
        try:
            src.add_file(path, discovered=discovered)
        except VhdlSerdesError as exc:
            if not discovered:
                raise
            src.warnings.append(f"fichier ignore: {exc}")
    src.finalize()
    return src
