"""Command line interface: vhdl-serdes / python -m vhdl_serdes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .generator import (GenOptions, generate, layout, record_width,
                        sort_records, static_sizes)
from .model import (DEFAULT_TYPE_SUFFIXES, Naming, RecordDef,
                    VhdlSerdesError)
from .parser import VhdlSource

DESCRIPTION = """\
Genere les fonctions VHDL de serialisation / deserialisation des records
trouves dans un ou plusieurs fichiers VHDL.

Pour chaque record de type <nom>_t (ou <nom>_type) sont generes :
  constant <NOM>_SERIALIZED_WIDTH : natural := ...;
  function <nom>_record2slv (value : <nom>_t) return std_logic_vector;
  function <nom>_slv2record (data : std_logic_vector) return <nom>_t;
Seul le debut du nom de type est conserve ; voir --keep-type-suffix.

Convention de rangement : le premier champ declare occupe les bits de poids
faible (offset 0), le dernier les bits de poids fort. Un record imbrique est
serialise via les fonctions de la meme convention de nommage ; si son type
n'est pas present dans les fichiers d'entree, ces fonctions sont supposees
exister (avertissement).
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vhdl-serdes",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", metavar="FICHIER.vhd",
                   help="fichier(s) VHDL contenant les declarations de record")
    p.add_argument("-o", "--output", metavar="FICHIER",
                   help="fichier VHDL genere (defaut: stdout)")
    p.add_argument("-l", "--list", action="store_true",
                   help="liste les records trouves et leur largeur, sans generer")
    p.add_argument("-p", "--package-name", metavar="NOM",
                   help="nom du package genere (defaut: <entree>_serdes_pkg)")
    p.add_argument("-r", "--record", action="append", default=[], metavar="NOM",
                   help="ne traiter que ce record (repetable ; les records "
                        "imbriques necessaires sont ajoutes automatiquement)")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="NOM",
                   help="exclure ce record (repetable)")

    g = p.add_argument_group("nommage")
    g.add_argument("--strip-type-prefix", action="append", default=[],
                   metavar="PREF",
                   help="prefixe du nom de type a retirer pour former le nom "
                        "des fonctions/constantes (repetable, ex: t_)")
    g.add_argument("--strip-type-suffix", action="append", default=None,
                   metavar="SUFF",
                   help="suffixe du nom de type a retirer (repetable ; defaut: "
                        + ", ".join(DEFAULT_TYPE_SUFFIXES) + ")")
    g.add_argument("--keep-type-suffix", action="store_true",
                   help="garder le nom de type complet (pas de retrait de "
                        "suffixe)")
    g.add_argument("--width-suffix", default="SERIALIZED_WIDTH", metavar="SUFF",
                   help="suffixe de la constante de largeur "
                        "(defaut: SERIALIZED_WIDTH)")
    g.add_argument("--serialize-suffix", default="record2slv", metavar="SUFF",
                   help="suffixe de la fonction de serialisation "
                        "(defaut: record2slv)")
    g.add_argument("--deserialize-suffix", default="slv2record", metavar="SUFF",
                   help="suffixe de la fonction de deserialisation "
                        "(defaut: slv2record)")

    g = p.add_argument_group("code genere")
    g.add_argument("--library", default="work", metavar="LIB",
                   help="bibliotheque des packages source (defaut: work)")
    g.add_argument("--use", action="append", default=[], metavar="PKG",
                   help="clause use supplementaire : 'pkg' -> use <lib>.pkg.all, "
                        "ou une clause complete 'use lib.pkg.all;'")
    g.add_argument("--no-auto-use", action="store_true",
                   help="ne pas deduire les clauses use des packages trouves "
                        "dans les fichiers d'entree")
    g.add_argument("--no-assert", action="store_true",
                   help="ne pas generer l'assertion de controle de largeur "
                        "dans les fonctions de deserialisation")
    p.add_argument("--strict", action="store_true",
                   help="traiter les avertissements (type inconnu suppose "
                        "record) comme des erreurs")
    return p


def _select(src: VhdlSource, only: list[str], exclude: list[str]) -> list[RecordDef]:
    known = src.records
    wanted: list[RecordDef] = []

    if only:
        for name in only:
            rec = known.get(name.lower())
            if rec is None:
                raise VhdlSerdesError(
                    f"record '{name}' introuvable ; disponibles: "
                    f"{', '.join(src.order) or '(aucun)'}")
            wanted.append(rec)
        # pull in nested records so the generated calls resolve
        queue = list(wanted)
        while queue:
            rec = queue.pop()
            for dep in rec.dependencies:
                child = known[dep.lower()]
                if child not in wanted:
                    wanted.append(child)
                    queue.append(child)
    else:
        wanted = [known[n.lower()] for n in src.order]

    excluded = {n.lower() for n in exclude}
    unknown = excluded - set(known)
    if unknown:
        raise VhdlSerdesError(
            f"record(s) a exclure introuvable(s): {', '.join(sorted(unknown))}")
    kept = [r for r in wanted if r.name.lower() not in excluded]

    for rec in kept:
        for dep in rec.dependencies:
            if dep.lower() in excluded:
                raise VhdlSerdesError(
                    f"'{rec.name}' contient le record '{dep}' qui est exclu ; "
                    f"les fonctions appelees ne seraient pas generees")
    if not kept:
        raise VhdlSerdesError("aucun record a generer apres filtrage")
    return kept


def _describe(records: list[RecordDef], naming: Naming) -> str:
    ordered = sort_records(records)
    sizes = static_sizes(ordered)
    out: list[str] = []
    for rec in ordered:
        bits = sizes.get(rec.name.lower())
        size = (f"{bits} bits" if bits is not None
                else f"{record_width(rec, naming).render()} bits")
        out.append(f"{rec.name}  ({rec.source}:{rec.line})  -> {size}")
        out.append(f"    {naming.width_const(rec.name)}")
        out.append(f"    {naming.serialize_fn(rec.name)} / "
                   f"{naming.deserialize_fn(rec.name)}")
        for fld, low, high in layout(rec, sizes):
            pos = (f"[{high:>4} : {low:>4}]" if low is not None and high is not None
                   else "[  dynamique  ]")
            extra = ""
            if fld.record_type:
                extra = "  (record externe)" if fld.external else "  (record)"
            out.append(f"    {pos}  {fld.name} : {fld.type_name}{extra}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.keep_type_suffix:
        suffixes: tuple[str, ...] = ()
    elif args.strip_type_suffix is None:
        suffixes = DEFAULT_TYPE_SUFFIXES
    else:
        suffixes = tuple(args.strip_type_suffix)

    naming = Naming(
        strip_prefixes=tuple(args.strip_type_prefix),
        strip_suffixes=suffixes,
        width_suffix=args.width_suffix,
        ser_suffix=args.serialize_suffix,
        deser_suffix=args.deserialize_suffix,
    )

    try:
        src = VhdlSource()
        for path in args.inputs:
            src.add_file(path)
        if not src.records:
            raise VhdlSerdesError(
                "aucun record trouve dans " + ", ".join(args.inputs))

        records = _select(src, args.record, args.exclude)

        for rec in records:
            if rec.package is None:
                src.warnings.append(
                    f"{rec.source}:{rec.line}: '{rec.name}' n'est pas declare "
                    f"dans un package ; le type ne sera pas visible depuis le "
                    f"package genere (deplacez-le ou ajoutez --use)")

        for warning in src.warnings:
            print(f"avertissement: {warning}", file=sys.stderr)
        if src.warnings and args.strict:
            raise VhdlSerdesError("--strict: arret sur avertissement")

        if args.list:
            print(_describe(records, naming))
            return 0

        stem = Path(args.inputs[0]).stem
        for tail in ("_pkg", "_package", "-pkg"):
            if stem.lower().endswith(tail):
                stem = stem[: -len(tail)]
                break
        default_pkg = stem + "_serdes_pkg"
        use_packages = [] if args.no_auto_use else list(src.packages)
        extra_use: list[str] = []
        for item in args.use:
            if item.strip().lower().startswith("use "):
                extra_use.append(item.strip())
            elif item not in use_packages:
                use_packages.append(item)

        opts = GenOptions(
            package_name=args.package_name or default_pkg,
            naming=naming,
            library=args.library,
            use_packages=use_packages,
            extra_use=extra_use,
            include_assert=not args.no_assert,
            sources=list(args.inputs),
        )
        code = generate(records, opts)

        if args.output:
            out = Path(args.output)
            if out.parent and not out.parent.exists():
                out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(code, encoding="utf-8", newline="\n")
            print(f"{out}: {len(records)} record(s) -> "
                  f"{opts.package_name}", file=sys.stderr)
        else:
            sys.stdout.write(code)
        return 0

    except VhdlSerdesError as exc:
        print(f"erreur: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
