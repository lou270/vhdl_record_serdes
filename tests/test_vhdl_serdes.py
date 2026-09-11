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
from vhdl_serdes.generator import layout, record_width, sort_records, static_sizes
from vhdl_serdes.parser import VhdlSource, strip_comments

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


def parse(text):
    src = VhdlSource()
    src.add_text(text, source="t.vhd")
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
                        "type arr is array (0 to 3) of std_logic;"
                        " type t is record s : arr; end record;",
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
        src = parse("type a_t is record x : b_t; end record;"
                    "type b_t is record y : a_t; end record;")
        # b_t is unknown while a_t is parsed, so close the loop explicitly
        src.records["b_t"].fields[0].external = False
        src.records["a_t"].fields[0].external = False
        src.records["a_t"].fields[0].record_type = "b_t"
        with self.assertRaises(VhdlSerdesError):
            sort_records([src.records["a_t"], src.records["b_t"]])


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
