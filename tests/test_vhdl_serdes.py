"""Tests: python -m unittest discover -s tests -t ."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhdl_serdes import FieldKind, GenOptions, Naming, VhdlSerdesError, generate
from vhdl_serdes.cli import main
from vhdl_serdes.generator import (element_base, field_width, layout,
                                   record_width, sort_records, static_sizes)
from vhdl_serdes.parser import (VhdlSource, collect_vhdl_files,
                                strip_comments)

SIMPLE = """
package p_pkg is
  type simple_t is record
    a : std_logic_vector(7 downto 0);
    b : unsigned(3 downto 0);
    c : signed(1 downto 0);
    v : std_logic;
  end record simple_t;
end package p_pkg;
"""

NESTED = """
package p_pkg is
  subtype byte_t is std_logic_vector(7 downto 0);
  type inner_t is record
    x : unsigned(15 downto 0);
  end record;
  type outer_t is record
    head : byte_t;
    sub  : inner_t;
    tail : std_logic;
  end record outer_t;
end package p_pkg;
"""


def parse(text, discovered=False):
    src = VhdlSource()
    src.add_text(text, source="t.vhd", discovered=discovered)
    src.finalize()
    return src


def gen(text, **kwargs):
    src = parse(text)
    records = [src.records[n.lower()] for n in src.order]
    opts = GenOptions(package_name="gen_pkg", sources=["t.vhd"], **kwargs)
    return generate(records, opts)


class TestParser(unittest.TestCase):
    def test_fields_and_kinds(self):
        rec = parse(SIMPLE).records["simple_t"]
        self.assertEqual([f.name for f in rec.fields], ["a", "b", "c", "v"])
        self.assertEqual([f.kind for f in rec.fields],
                         [FieldKind.STD_LOGIC_VECTOR, FieldKind.UNSIGNED,
                          FieldKind.SIGNED, FieldKind.STD_LOGIC])
        self.assertEqual([f.width.const for f in rec.fields], [8, 4, 2, 1])
        self.assertEqual(rec.package, "p_pkg")

    def test_grouped_field_declaration(self):
        rec = parse("""
            type t is record
              a, b : std_logic;
              c    : unsigned(2 downto 0);
            end record;
        """).records["t"]
        self.assertEqual([f.name for f in rec.fields], ["a", "b", "c"])
        self.assertEqual(sum(f.width.const for f in rec.fields), 5)

    def test_subtype_and_nesting(self):
        src = parse(NESTED)
        outer = src.records["outer_t"]
        self.assertEqual(outer.fields[0].kind, FieldKind.STD_LOGIC_VECTOR)
        self.assertEqual(outer.fields[0].width.const, 8)
        self.assertEqual(outer.fields[1].kind, FieldKind.RECORD)
        self.assertEqual(outer.fields[1].record_type, "inner_t")
        self.assertFalse(outer.fields[1].external)
        self.assertEqual(outer.dependencies, ["inner_t"])
        self.assertEqual(src.warnings, [])

    def test_unknown_type_assumed_record(self):
        src = parse("type t is record s : foreign_t; end record;")
        fld = src.records["t"].fields[0]
        self.assertEqual(fld.kind, FieldKind.RECORD)
        self.assertTrue(fld.external)
        self.assertEqual(src.records["t"].dependencies, [])  # nothing to generate
        self.assertEqual(len(src.warnings), 1)

    def test_symbolic_bounds(self):
        rec = parse("type t is record d : std_logic_vector(W - 1 downto 0); "
                    "end record;").records["t"]
        self.assertFalse(rec.fields[0].width.is_static)
        self.assertEqual(rec.fields[0].width.render(), "((W - 1) + 1)")

    def test_descending_bounds_and_to_direction(self):
        rec = parse("type t is record a : std_logic_vector(0 to 5); "
                    "b : unsigned(2*4-1 downto 0); end record;").records["t"]
        self.assertEqual([f.width.const for f in rec.fields], [6, 8])

    def test_comments_are_ignored(self):
        rec = parse("""
            type t is record
              a : std_logic;  -- a : std_logic_vector(7 downto 0);
              -- b : std_logic;
              c : std_logic;
            end record;
        """).records["t"]
        self.assertEqual([f.name for f in rec.fields], ["a", "c"])

    def test_strip_comments_keeps_offsets_and_strings(self):
        text = 'a := "--x"; -- gone\nb;'
        stripped = strip_comments(text)
        self.assertEqual(len(stripped), len(text))
        self.assertIn('"--x"', stripped)
        self.assertNotIn("gone", stripped)

    def test_unsupported_types(self):
        for snippet in ("type t is record n : integer; end record;",
                        "type t is record f : boolean; end record;",
                        "type e is (a, b); type t is record s : e; end record;",
                        "type grid is array (0 to 1, 0 to 2) of std_logic;"
                        " type t is record s : grid; end record;",
                        "type row is array (natural range <>) of std_logic;"
                        " type grid is array (natural range <>) of row(0 to 3);"
                        " type t is record s : grid(0 to 1); end record;",
                        "type t is record d : std_logic_vector; end record;"):
            with self.subTest(snippet=snippet):
                with self.assertRaises(VhdlSerdesError):
                    parse(snippet)

    def test_duplicate_field_and_record(self):
        with self.assertRaises(VhdlSerdesError):
            parse("type t is record a : std_logic; a : std_logic; end record;")
        with self.assertRaises(VhdlSerdesError):
            parse("type t is record a : std_logic; end record;"
                  "type t is record b : std_logic; end record;")

    def test_empty_record_rejected(self):
        with self.assertRaises(VhdlSerdesError):
            parse("type t is record end record;")


class TestLayout(unittest.TestCase):
    def test_first_field_on_lsb(self):
        rec = parse(SIMPLE).records["simple_t"]
        rows = layout(rec, static_sizes([rec]))
        self.assertEqual([(f.name, lo, hi) for f, lo, hi in rows],
                         [("a", 0, 7), ("b", 8, 11), ("c", 12, 13), ("v", 14, 14)])
        self.assertEqual(record_width(rec, Naming()).const, 15)

    def test_nested_width_is_resolved_statically(self):
        src = parse(NESTED)
        records = [src.records["inner_t"], src.records["outer_t"]]
        sizes = static_sizes(records)
        self.assertEqual(sizes["inner_t"], 16)
        self.assertEqual(sizes["outer_t"], 25)
        rows = layout(src.records["outer_t"], sizes)
        self.assertEqual([(f.name, lo, hi) for f, lo, hi in rows],
                         [("head", 0, 7), ("sub", 8, 23), ("tail", 24, 24)])

    def test_external_record_keeps_width_symbolic(self):
        rec = parse("type t is record s : foreign_t; f : std_logic; "
                    "end record;").records["t"]
        self.assertEqual(static_sizes([rec]), {})
        self.assertEqual(record_width(rec, Naming()).render(),
                         "FOREIGN_SERIALIZED_WIDTH + 1")

    def test_topological_order(self):
        src = parse(NESTED)
        records = [src.records["outer_t"], src.records["inner_t"]]
        self.assertEqual([r.name for r in sort_records(records)],
                         ["inner_t", "outer_t"])

    def test_cycle_detected(self):
        # both records are known before their bodies are resolved, so the loop
        # is a genuine one rather than a pair of unknown types
        src = parse("type a_t is record x : b_t; end record;"
                    "type b_t is record y : a_t; end record;")
        self.assertEqual(src.records["a_t"].dependencies, ["b_t"])
        with self.assertRaises(VhdlSerdesError):
            sort_records([src.records["a_t"], src.records["b_t"]])


ARRAYS = """
package p_pkg is
  type coord_t is record
    x : signed(11 downto 0);
    y : signed(11 downto 0);
  end record coord_t;

  type coord_vector is array (natural range <>) of coord_t;
  type byte_vector  is array (natural range <>) of std_logic_vector(7 downto 0);
  type flag_vector  is array (natural range <>) of std_logic;

  type blob_t is record
    corners : coord_vector(0 to 3);
    mask    : byte_vector(0 to 1);
    flags   : flag_vector(0 to 2);
    alive   : std_logic;
  end record blob_t;
end package p_pkg;
"""


class TestArrayFields(unittest.TestCase):
    def test_kinds_and_counts(self):
        blob = parse(ARRAYS).records["blob_t"]
        self.assertEqual([f.kind for f in blob.fields],
                         [FieldKind.RECORD_VECTOR, FieldKind.SCALAR_VECTOR,
                          FieldKind.SCALAR_VECTOR, FieldKind.STD_LOGIC])
        corners, mask, flags, _ = blob.fields
        self.assertEqual(corners.array.count.const, 4)
        self.assertEqual(corners.array.element_record, "coord_t")
        self.assertEqual(corners.array.vector_type, "coord_vector")
        self.assertFalse(corners.array.descending)
        self.assertEqual(mask.array.element_width.const, 8)
        self.assertEqual(flags.array.element_width.const, 1)
        self.assertEqual(blob.dependencies, ["coord_t"])

    def test_widths_and_layout(self):
        src = parse(ARRAYS)
        records = [src.records["coord_t"], src.records["blob_t"]]
        sizes = static_sizes(records)
        self.assertEqual(sizes["blob_t"], 4 * 24 + 2 * 8 + 3 + 1)
        rows = layout(src.records["blob_t"], sizes)
        self.assertEqual([(f.name, lo, hi) for f, lo, hi in rows],
                         [("corners", 0, 95), ("mask", 96, 111),
                          ("flags", 112, 114), ("alive", 115, 115)])

    def test_symbolic_count(self):
        rec = parse("type coord_t is record x : std_logic; end record;"
                    "type coord_vector is array (natural range <>) of coord_t;"
                    "type t is record c : coord_vector(0 to N - 1); "
                    "end record;").records["t"]
        self.assertEqual(field_width(rec.fields[0], Naming()).render(),
                         "(((N - 1) + 1) * COORD_SERIALIZED_WIDTH)")

    def test_descending_range(self):
        rec = parse("type coord_t is record x : std_logic; end record;"
                    "type coord_vector is array (natural range <>) of coord_t;"
                    "type t is record c : coord_vector(3 downto 0); "
                    "end record;").records["t"]
        self.assertTrue(rec.fields[0].array.descending)
        self.assertEqual(rec.fields[0].array.count.const, 4)

    def test_constrained_array_type(self):
        src = parse("type coord_t is record x : std_logic; end record;"
                    "type coord_vector is array (0 to 3) of coord_t;"
                    "type t is record c : coord_vector; end record;")
        info = src.records["t"].fields[0].array
        self.assertEqual(info.count.const, 4)
        self.assertFalse(info.type_unconstrained)
        self.assertFalse(src.records["coord_t"].vector.unconstrained)

    def test_range_errors(self):
        base = ("type coord_t is record x : std_logic; end record;"
                "type coord_vector is array (%s) of coord_t;"
                "type t is record c : coord_vector%s; end record;")
        with self.assertRaises(VhdlSerdesError):   # already constrained
            parse(base % ("0 to 3", "(0 to 3)"))
        with self.assertRaises(VhdlSerdesError):   # still unconstrained
            parse(base % ("natural range <>", ""))

    def test_record_with_a_range_is_refused_with_a_hint(self):
        with self.assertRaises(VhdlSerdesError) as ctx:
            parse("type coord_t is record x : std_logic; end record;"
                  "type t is record c : coord_t(0 to 3); end record;")
        self.assertIn("coord_vector", str(ctx.exception))

    def test_external_vector_follows_the_convention(self):
        src = parse("type t is record c : payload_vector(0 to 7); end record;")
        fld = src.records["t"].fields[0]
        self.assertEqual(fld.kind, FieldKind.RECORD_VECTOR)
        self.assertTrue(fld.external)
        self.assertEqual(fld.array.count.const, 8)
        self.assertEqual(element_base(fld, Naming()), "payload")
        self.assertEqual(len(src.warnings), 1)

    def test_vector_type_linked_to_its_record(self):
        src = parse(ARRAYS)
        self.assertEqual(src.records["coord_t"].vector.name, "coord_vector")
        self.assertTrue(src.records["coord_t"].vector.unconstrained)
        self.assertIsNone(src.records["blob_t"].vector)

    def test_off_convention_vector_name_warns(self):
        src = parse("type coord_t is record x : std_logic; end record;"
                    "type coord_array_t is array (natural range <>) of coord_t;"
                    "type t is record c : coord_array_t(0 to 1); end record;")
        self.assertEqual(len(src.warnings), 1)
        self.assertIn("coord_vector", src.warnings[0])
        # the functions still take the type that actually exists
        self.assertEqual(src.records["t"].fields[0].array.vector_type,
                         "coord_array_t")


class TestArrayGeneration(unittest.TestCase):
    def test_vector_functions_declared(self):
        code = gen(ARRAYS)
        self.assertIn("function coord_recordvector2slv (value : coord_vector) "
                      "return std_logic_vector;", code)
        self.assertIn("function coord_slv2recordvector (data : std_logic_vector) "
                      "return coord_vector;", code)
        # blob_t has no array type of its own
        self.assertNotIn("blob_recordvector2slv", code)

    def test_record_array_field_uses_the_vector_functions(self):
        code = gen(ARRAYS)
        self.assertIn("coord_recordvector2slv(value.corners)", code)
        self.assertIn("coord_slv2recordvector(src(BLOB_CORNERS_HIGH downto "
                      "BLOB_CORNERS_LOW))", code)
        self.assertIn("constant BLOB_SERIALIZED_WIDTH : natural := "
                      "(4 * COORD_SERIALIZED_WIDTH) + 16 + 3 + 1;", code)

    def test_vector_functions_pack_lowest_index_first(self):
        code = gen(ARRAYS)
        self.assertIn("element_low := (i - value'low) * "
                      "COORD_SERIALIZED_WIDTH;", code)
        self.assertIn("element_low := (i - result'low) * "
                      "COORD_SERIALIZED_WIDTH;", code)

    def test_scalar_array_loops(self):
        code = gen(ARRAYS)
        self.assertIn("for i in value.mask'range loop", code)
        self.assertIn("element_low := BLOB_MASK_LOW + "
                      "(i - value.mask'low) * 8;", code)
        self.assertIn("result(element_low + 7 downto element_low) := "
                      "value.mask(i);", code)
        self.assertIn("result.mask(i) := src(element_low + 7 downto "
                      "element_low);", code)
        # single-bit elements need no stride multiplication
        self.assertIn("element_low := BLOB_FLAGS_LOW + "
                      "(i - value.flags'low);", code)
        self.assertIn("result(element_low) := value.flags(i);", code)

    def test_descending_field_keeps_index_order(self):
        code = gen("type coord_t is record x : std_logic; end record;"
                   "type coord_vector is array (natural range <>) of coord_t;"
                   "type t is record c : coord_vector(3 downto 0); end record;")
        self.assertRegex(code, r"variable c_v\s+: coord_vector\(0 to 3\);")
        self.assertIn("c_v := coord_slv2recordvector(", code)
        self.assertIn("result.c(i) := c_v(i - result.c'low);", code)

    def test_constrained_vector_type_bodies(self):
        code = gen("type coord_t is record x : std_logic; end record;"
                   "type coord_vector is array (0 to 3) of coord_t;"
                   "type t is record c : coord_vector; end record;")
        self.assertRegex(code, r"variable result\s+: coord_vector;")
        self.assertNotIn("constant COUNT", code)
        self.assertIn("assert data'length = result'length * "
                      "COORD_SERIALIZED_WIDTH", code)

    def test_generated_code_stays_ascii(self):
        gen(ARRAYS).encode("ascii")


class TestNaming(unittest.TestCase):
    def test_default_drops_type_tag(self):
        naming = Naming()
        for type_name, base in (("frame_t", "frame"), ("frame_type", "frame"),
                                ("frame_T", "frame"), ("axi_stream_t", "axi_stream"),
                                ("frame", "frame"), ("frame_rec", "frame_rec")):
            with self.subTest(type_name=type_name):
                self.assertEqual(naming.base(type_name), base)

    def test_longest_suffix_wins(self):
        self.assertEqual(Naming(strip_suffixes=("_t", "_t_rec")).base("a_t_rec"),
                         "a")

    def test_affix_never_eats_whole_name(self):
        self.assertEqual(Naming().base("_t"), "_t")
        self.assertEqual(Naming(strip_prefixes=("t_",)).base("t_"), "t_")

    def test_prefix_and_suffix_together(self):
        naming = Naming(strip_prefixes=("t_",))
        self.assertEqual(naming.base("t_frame_type"), "frame")

    def test_generated_identifiers(self):
        naming = Naming()
        self.assertEqual(naming.width_const("frame_type"),
                         "FRAME_SERIALIZED_WIDTH")
        self.assertEqual(naming.serialize_fn("frame_t"), "frame_record2slv")
        self.assertEqual(naming.deserialize_fn("frame_t"), "frame_slv2record")
        self.assertEqual(naming.bound_const("frame_t", "addr", "LOW"),
                         "FRAME_ADDR_LOW")

    def test_keeping_the_whole_type_name(self):
        naming = Naming(strip_suffixes=())
        self.assertEqual(naming.serialize_fn("frame_t"), "frame_t_record2slv")

    def test_colliding_base_names_rejected(self):
        with self.assertRaises(VhdlSerdesError) as ctx:
            gen("package p_pkg is "
                "type frame_t is record a : std_logic; end record;"
                "type frame_type is record b : std_logic; end record;"
                "end package;")
        self.assertIn("collision", str(ctx.exception))


class TestGenerator(unittest.TestCase):
    def test_declarations(self):
        code = gen(SIMPLE)
        self.assertIn("constant SIMPLE_SERIALIZED_WIDTH : natural "
                      ":= 8 + 4 + 2 + 1;", code)
        self.assertIn("function simple_record2slv (value : simple_t) "
                      "return std_logic_vector;", code)
        self.assertIn("function simple_slv2record (data : std_logic_vector) "
                      "return simple_t;", code)
        self.assertIn("package gen_pkg is", code)
        self.assertIn("end package body gen_pkg;", code)
        self.assertIn("use work.p_pkg.all", gen(SIMPLE, use_packages=["p_pkg"]))

    def test_offsets_start_at_zero_for_first_field(self):
        code = gen(SIMPLE)
        self.assertRegex(code, r"constant SIMPLE_A_LOW\s+: natural := 0;")
        self.assertRegex(
            code, r"constant SIMPLE_B_LOW\s+: natural := SIMPLE_A_HIGH \+ 1;")

    def test_conversions(self):
        code = gen(SIMPLE)
        self.assertIn("result(SIMPLE_A_HIGH downto SIMPLE_A_LOW) := value.a;",
                      code)
        self.assertIn("std_logic_vector(value.b)", code)   # unsigned -> slv
        self.assertIn("std_logic_vector(value.c)", code)   # signed   -> slv
        self.assertIn("result(SIMPLE_V_LOW)", code)      # std_logic, single bit
        self.assertIn("result.b := unsigned(src(", code)
        self.assertIn("result.c := signed(src(", code)
        self.assertIn("result.v := src(SIMPLE_V_LOW);", code)

    def test_single_bit_field_has_no_high_constant(self):
        code = gen(SIMPLE)
        self.assertNotIn("SIMPLE_V_HIGH", code)

    def test_nested_calls_the_convention(self):
        code = gen(NESTED)
        self.assertIn("constant OUTER_SERIALIZED_WIDTH : natural "
                      ":= 8 + INNER_SERIALIZED_WIDTH + 1;", code)
        self.assertIn("inner_record2slv(value.sub)", code)
        self.assertRegex(code, r"result\.sub\s+:= inner_slv2record\(src\(")
        # inner_t must be declared before outer_t uses its width constant
        self.assertLess(code.index("constant INNER_SERIALIZED_WIDTH"),
                        code.index("constant OUTER_SERIALIZED_WIDTH"))

    def test_assert_can_be_disabled(self):
        self.assertIn("severity failure;", gen(SIMPLE))
        self.assertNotIn("severity failure;", gen(SIMPLE, include_assert=False))

    def test_naming_options(self):
        code = gen(SIMPLE, naming=Naming(strip_suffixes=("_t",),
                                         width_suffix="SER_W",
                                         ser_suffix="to_slv",
                                         deser_suffix="from_slv"))
        self.assertIn("constant SIMPLE_SER_W : natural", code)
        self.assertIn("function simple_to_slv (value : simple_t)", code)
        self.assertIn("function simple_from_slv (data : std_logic_vector)", code)

    def test_every_field_is_both_written_and_read(self):
        code = gen(NESTED)
        for rec_name, fields in (("inner_t", ["x"]),
                                 ("outer_t", ["head", "sub", "tail"])):
            for name in fields:
                with self.subTest(record=rec_name, field=name):
                    self.assertIn(f"result.{name} ", code.replace(
                        f"result.{name}:", f"result.{name} :"))
                    self.assertIn(f"value.{name}", code)

    def test_generated_lines_are_ascii(self):
        code = gen(NESTED)
        code.encode("ascii")  # raises if a non-ASCII character slipped in


class TestDirectoryScan(unittest.TestCase):
    """Whole-directory input: discovery, file order, tolerance."""

    FILES = {
        # read first alphabetically, but depends on a record declared below
        "rtl/a_top_pkg.vhd": """
            package a_top_pkg is
              type frame_t is record
                hdr  : header_t;
                last : std_logic;
              end record frame_t;
            end package a_top_pkg;
        """,
        "rtl/common/z_header_pkg.vhd": """
            package z_header_pkg is
              type header_t is record
                id    : unsigned(7 downto 0);
                valid : std_logic;
              end record header_t;
            end package z_header_pkg;
        """,
        # one unusable record, one fine, one depending on the unusable one
        "rtl/common/misc_pkg.vhd": """
            package misc_pkg is
              type stats_t is record
                count : integer;
              end record stats_t;
              type good_t is record
                flag : std_logic;
              end record good_t;
              type uses_stats_t is record
                s : stats_t;
              end record uses_stats_t;
            end package misc_pkg;
        """,
        "doc/notes.txt": "pas du vhdl",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name, text in self.FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def _run(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(args)
        return rc, out.getvalue(), err.getvalue()

    def test_collect_only_vhdl_files(self):
        found = collect_vhdl_files([str(self.root)])
        names = sorted(p.name for p, _ in found)
        self.assertEqual(names, ["a_top_pkg.vhd", "misc_pkg.vhd",
                                 "z_header_pkg.vhd"])
        self.assertTrue(all(discovered for _, discovered in found))

    def test_explicit_file_is_not_flagged_discovered(self):
        path = self.root / "rtl" / "a_top_pkg.vhd"
        self.assertEqual(collect_vhdl_files([str(path)]), [(path.resolve(), False)])

    def test_no_recursive(self):
        with self.assertRaises(VhdlSerdesError):
            collect_vhdl_files([str(self.root)], recursive=False)
        found = collect_vhdl_files([str(self.root / "rtl")], recursive=False)
        self.assertEqual([p.name for p, _ in found], ["a_top_pkg.vhd"])

    def test_custom_extension(self):
        (self.root / "extra.vhdl").write_text(
            "package e_pkg is end package;", encoding="utf-8")
        found = collect_vhdl_files([str(self.root)], extensions=(".vhdl",))
        self.assertEqual([p.name for p, _ in found], ["extra.vhdl"])

    def test_duplicates_are_collapsed(self):
        path = self.root / "rtl" / "a_top_pkg.vhd"
        found = collect_vhdl_files([str(path), str(self.root)])
        self.assertEqual(len(found), 3)
        self.assertFalse(dict(found)[path.resolve()])  # explicit wins

    def test_missing_path(self):
        with self.assertRaises(VhdlSerdesError):
            collect_vhdl_files([str(self.root / "nope")])

    def test_record_is_resolved_across_files_whatever_the_order(self):
        rc, out, err = self._run([str(self.root), "--list"])
        self.assertEqual(rc, 0)
        self.assertIn("3 fichier(s) lu(s), 3 record(s) trouve(s)", err)
        self.assertIn("hdr : header_t  (record)", out)

    def test_generated_widths_follow_dependencies(self):
        rc, out, _ = self._run([str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn("constant FRAME_SERIALIZED_WIDTH : natural := "
                      "HEADER_SERIALIZED_WIDTH + 1;", out)
        self.assertLess(out.index("HEADER_SERIALIZED_WIDTH : natural"),
                        out.index("FRAME_SERIALIZED_WIDTH : natural"))

    def test_unusable_records_are_skipped_with_their_dependents(self):
        rc, out, err = self._run([str(self.root), "--list"])
        self.assertEqual(rc, 0)
        self.assertIn("record 'stats_t' ignore", err)
        self.assertIn("type 'integer' non supporte", err)
        self.assertIn("record 'uses_stats_t' ignore", err)
        self.assertIn("contient le record 'stats_t' qui a ete ignore", err)
        self.assertIn("good_t", out)        # the rest of the file is kept
        self.assertNotIn("stats_record2slv", out)

    def test_strict_stops_on_a_skipped_record(self):
        rc, _, _ = self._run([str(self.root), "--list", "--strict"])
        self.assertEqual(rc, 1)

    def test_the_same_file_named_explicitly_still_fails(self):
        path = self.root / "rtl" / "common" / "misc_pkg.vhd"
        rc, _, err = self._run([str(path), "--list"])
        self.assertEqual(rc, 1)
        self.assertIn("type 'integer' non supporte", err)

    def test_selecting_a_skipped_record_says_why(self):
        rc, _, err = self._run([str(self.root), "-r", "stats_t"])
        self.assertEqual(rc, 1)
        self.assertIn("record 'stats_t' ignore", err)

    def test_output_file_is_not_scanned_back(self):
        out_file = self.root / "rtl" / "gen_serdes_pkg.vhd"
        rc, _, err = self._run([str(self.root), "-o", str(out_file)])
        self.assertEqual(rc, 0)
        rc, _, err = self._run([str(self.root), "-o", str(out_file)])
        self.assertEqual(rc, 0)
        self.assertIn("3 fichier(s) lu(s)", err)   # not 4
        self.assertNotIn("use work.rtl_serdes_pkg.all;",
                         out_file.read_text(encoding="utf-8"))

    def test_package_name_comes_from_the_directory(self):
        rc, out, _ = self._run([str(self.root / "rtl")])
        self.assertEqual(rc, 0)
        self.assertIn("package rtl_serdes_pkg is", out)

    def test_duplicate_record_across_discovered_files(self):
        (self.root / "rtl" / "dup_pkg.vhd").write_text(
            "package dup_pkg is type header_t is record "
            "other : std_logic; end record; end package;", encoding="utf-8")
        rc, out, err = self._run([str(self.root), "--list"])
        self.assertEqual(rc, 0)
        self.assertIn("est deja defini dans", err)
        self.assertIn("la seconde est ignoree", err)


class TestCli(unittest.TestCase):
    def _run(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(args)
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "dut_pkg.vhd"
        self.src.write_text(NESTED, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_stdout_generation(self):
        rc, out, _ = self._run([str(self.src)])
        self.assertEqual(rc, 0)
        self.assertIn("package dut_serdes_pkg is", out)

    def test_output_file(self):
        dst = Path(self.tmp.name) / "out" / "gen.vhd"
        rc, _, err = self._run([str(self.src), "-o", str(dst)])
        self.assertEqual(rc, 0)
        self.assertTrue(dst.is_file())
        self.assertIn("2 record(s)", err)

    def test_list_mode(self):
        rc, out, _ = self._run([str(self.src), "--list"])
        self.assertEqual(rc, 0)
        self.assertIn("outer_t", out)
        self.assertIn("25 bits", out)

    def test_only_pulls_dependencies(self):
        rc, out, _ = self._run([str(self.src), "-r", "outer_t"])
        self.assertEqual(rc, 0)
        self.assertIn("inner_record2slv", out)

    def test_exclude_needed_record_fails(self):
        rc, _, err = self._run([str(self.src), "-x", "inner_t"])
        self.assertEqual(rc, 1)
        self.assertIn("exclu", err)

    def test_strict_turns_warning_into_error(self):
        path = Path(self.tmp.name) / "ext_pkg.vhd"
        path.write_text("type t is record s : foreign_t; end record;",
                        encoding="utf-8")
        rc, _, err = self._run([str(path)])
        self.assertEqual(rc, 0)
        self.assertIn("avertissement", err)
        rc, _, err = self._run([str(path), "--strict"])
        self.assertEqual(rc, 1)

    def test_out_of_package_record_warns(self):
        path = Path(self.tmp.name) / "loose.vhd"
        path.write_text("architecture a of e is\n"
                        " type loose_t is record b : std_logic; end record;\n"
                        "begin end architecture;\n", encoding="utf-8")
        rc, _, err = self._run([str(path)])
        self.assertEqual(rc, 0)
        self.assertIn("n'est pas declare dans un package", err)

    def test_external_record_uses_same_convention(self):
        path = Path(self.tmp.name) / "ext2_pkg.vhd"
        path.write_text("package ext2_pkg is type t_frame is record "
                        "tag : t_meta; end record; end package;",
                        encoding="utf-8")
        rc, out, _ = self._run([str(path), "--strip-type-prefix", "t_"])
        self.assertEqual(rc, 0)
        self.assertIn("META_SERIALIZED_WIDTH", out)
        self.assertIn("meta_record2slv(value.tag)", out)
        self.assertIn("meta_slv2record(src(", out)
        self.assertIn("function frame_record2slv (value : t_frame)", out)

    def test_keep_type_suffix_option(self):
        rc, out, _ = self._run([str(self.src)])
        self.assertIn("function outer_record2slv (value : outer_t)", out)
        rc2, out2, _ = self._run([str(self.src), "--keep-type-suffix"])
        self.assertEqual((rc, rc2), (0, 0))
        self.assertIn("function outer_t_record2slv (value : outer_t)", out2)

    def test_custom_strip_suffix_overrides_default(self):
        path = Path(self.tmp.name) / "custom_pkg.vhd"
        path.write_text("package custom_pkg is type frame_rec is record "
                        "a : std_logic; end record; end package;",
                        encoding="utf-8")
        rc, out, _ = self._run([str(path), "--strip-type-suffix", "_rec"])
        self.assertEqual(rc, 0)
        self.assertIn("function frame_record2slv (value : frame_rec)", out)
        self.assertIn("constant FRAME_SERIALIZED_WIDTH", out)

    def test_list_shows_vector_functions(self):
        path = Path(self.tmp.name) / "vec_pkg.vhd"
        path.write_text(ARRAYS, encoding="utf-8")
        rc, out, _ = self._run([str(path), "--list"])
        self.assertEqual(rc, 0)
        self.assertIn("coord_recordvector2slv / coord_slv2recordvector", out)
        self.assertIn("(sur coord_vector)", out)

    def test_missing_vector_type_is_reported(self):
        path = Path(self.tmp.name) / "miss_pkg.vhd"
        path.write_text("package miss_pkg is "
                        "type coord_t is record x : std_logic; end record;"
                        "type t is record c : coord_vector(0 to 1); end record;"
                        "end package;", encoding="utf-8")
        rc, out, err = self._run([str(path)])
        self.assertEqual(rc, 0)
        self.assertIn("coord_recordvector2slv", out)  # assumed to exist
        self.assertIn("n'est declare dans aucun fichier", err)
        self.assertIn("type coord_vector is array (natural range <>) of "
                      "coord_t;", err)

    def test_missing_file(self):
        rc, _, err = self._run(["nope.vhd"])
        self.assertEqual(rc, 1)
        self.assertIn("introuvable", err)

    def test_no_record_found(self):
        path = Path(self.tmp.name) / "empty_pkg.vhd"
        path.write_text("package empty_pkg is end package;", encoding="utf-8")
        rc, _, err = self._run([str(path)])
        self.assertEqual(rc, 1)
        self.assertIn("aucun record", err)


if __name__ == "__main__":
    unittest.main()
