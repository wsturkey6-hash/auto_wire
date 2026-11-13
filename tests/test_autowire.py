"""Tests for autowire_v3.

Run with:  python -m unittest discover -s tests   (from the repo root)

Covers unit-level helpers, an end-to-end wiring run on a hermetic synthetic
fixture (assertions on the generated RTL), idempotency (a second run is
byte-identical), pyslang elaboration of the result, the missing-source fatal
path, the pyslang import-failure messages, regression cases from the 2026-10
review (routing up through intermediate levels, tapping already-wired outputs,
parameterized modules and instances, multi-module files, file encodings and
line endings; simulated with iverilog+vvp when available), and an optional
external linter cross-check (skipped if none installed).
"""
import os
import re
import sys
import contextlib
import glob
import io
import json
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import autowire_v3 as aw  # noqa: E402

AUTOWIRE = str(ROOT / "autowire_v3.py")


# ── fixture + helpers ────────────────────────────────────────────────────────
def write_fixture(d: Path):
    """A 3-level design: top -> u_mid -> u_leaf, all connected only by clk."""
    (d / "leaf.v").write_text(textwrap.dedent("""\
        module leaf (
            input clk
        );
        endmodule
    """), newline="\n")
    (d / "mid.v").write_text(textwrap.dedent("""\
        module mid (
            input clk
        );
            leaf u_leaf (.clk(clk));
        endmodule
    """), newline="\n")
    (d / "top.v").write_text(textwrap.dedent("""\
        module top (
            input        clk,
            input  [7:0] data_in
        );
            mid u_mid (.clk(clk));
        endmodule
    """), newline="\n")


def write_fixture_nonansi(d: Path):
    """Same 3-level design in legacy (non-ANSI) style: port names in the header,
    I/O declared in the body. Exercises the bare-name header path."""
    (d / "leaf.v").write_text(textwrap.dedent("""\
        module leaf (clk);
            input clk;
        endmodule
    """), newline="\n")
    (d / "mid.v").write_text(textwrap.dedent("""\
        module mid (clk);
            input clk;
            leaf u_leaf (.clk(clk));
        endmodule
    """), newline="\n")
    (d / "top.v").write_text(textwrap.dedent("""\
        module top (clk, data_in);
            input        clk;
            input  [7:0] data_in;
            mid u_mid (.clk(clk));
        endmodule
    """), newline="\n")


def run_tool(rtldir, csvpath, top="top", extra=(), answer="y\n", cwd=None,
             env_extra=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    # Keep the warning log next to the fixture instead of the caller's cwd.
    return subprocess.run(
        [sys.executable, AUTOWIRE, "-d", str(rtldir), "-T", top,
         "-c", str(csvpath), "--no-color",
         "--out-log", str(Path(rtldir) / "autowire_warn.log"), *extra],
        input=answer, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, cwd=cwd,
    )


def read_all(rtldir):
    return {Path(f).name: Path(f).read_text(encoding="utf-8", errors="replace")
            for f in sorted(glob.glob(os.path.join(str(rtldir), "*.v")))}


def elaboration_errors(rtldir):
    """Number of error-severity diagnostics when elaborating rtldir with pyslang."""
    import pyslang
    from pyslang.syntax import SyntaxTree
    from pyslang.ast import Compilation
    sm = pyslang.SourceManager()
    sm.addUserDirectories(str(rtldir))
    comp = Compilation()
    for f in sorted(glob.glob(os.path.join(str(rtldir), "*.v"))
                    + glob.glob(os.path.join(str(rtldir), "*.sv"))):
        comp.addSyntaxTree(SyntaxTree.fromFile(f, sm))
    comp.getRoot()
    eng = pyslang.DiagnosticEngine(sm)
    for dgn in comp.getAllDiagnostics():
        eng.issue(dgn)
    n = eng.numErrors
    return n() if callable(n) else n


# ── unit tests ───────────────────────────────────────────────────────────────
class UnitTests(unittest.TestCase):
    def test_lca(self):
        self.assertEqual(aw._lca("TOP/a/b", "TOP/a/c"), "TOP/a")
        self.assertEqual(aw._lca("TOP", "TOP/a"), "TOP")
        self.assertEqual(aw._lca("TOP/a", "TOP/a"), "TOP/a")

    def test_levels_between(self):
        self.assertEqual(aw._levels_between("TOP/a/b", "TOP"), ["a", "b"])
        self.assertEqual(aw._levels_between("TOP", "TOP"), [])

    def test_parse_endpoint(self):
        self.assertEqual(aw._parse_endpoint("TOP/u.port", 1), ("TOP/u", "port"))
        self.assertEqual(aw._parse_endpoint("TOP/u/port", 1), ("TOP/u", "port"))
        with self.assertRaises(ValueError):
            aw._parse_endpoint("noseparator", 1)

    def test_csv_parse_autoname_and_bitwidth(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            p.write_text(
                "wire_name,bit_width,src,dst,comment\n"
                "w_x,8,TOP/a.o,TOP/b.i,bus\n"
                ",1,TOP/a.q,TOP/b.r,auto\n", newline="\n")
            conns = aw.parse_connections_csv(str(p))
            self.assertEqual(len(conns), 2)
            self.assertEqual(conns[0].wire_name, "w_x")
            self.assertEqual(conns[0].bit_width, 8)
            self.assertEqual(conns[1].wire_name, "w_q_to_r")  # auto-generated

    def test_mask_comments_and_strings(self):
        m = aw._mask_comments
        # A string's contents are blanked, its quotes kept; nothing inside it
        # starts a comment.
        self.assertEqual(m('.a ("(//)") // c'), '.a ("    ")     ')
        self.assertEqual(m('"/*" (x) "*/"'), '"  " (x) "  "')
        # Escaped quote, escaped backslash, backslash-newline continuation.
        self.assertEqual(m('"a\\"b" ('), '"    " (')
        self.assertEqual(m('"a\\\\" )'), '"   " )')
        self.assertEqual(m('"a\\\nb" )'), '"    " )')
        # An unterminated string ends at the line.
        self.assertEqual(m('"ab\n(c)'), '"  \n(c)')
        # A quote inside a comment, or a quote or `//` inside an escaped
        # identifier, starts nothing.
        self.assertEqual(m('/* " */ (x)'), '        (x)')
        self.assertEqual(m('\\a"b//c (x)'), '\\a"b//c (x)')


# ── end-to-end tests ─────────────────────────────────────────────────────────
class _E2EBase:
    """Shared end-to-end checks; subclasses pick the fixture style."""
    make_fixture = staticmethod(write_fixture)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rtl = Path(self.tmp)
        type(self).make_fixture(self.rtl)
        self.csv = self.rtl / "conn.csv"
        self.csv.write_text(
            "wire_name,bit_width,src,dst,comment\n"
            "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,route down\n",
            newline="\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_src_at_lca_drives_wire_and_threads_down(self):
        r = run_tool(self.rtl, self.csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        files = read_all(self.rtl)
        # source at the LCA is driven (the bug this whole project started from)
        self.assertIn("assign w_data = data_in", files["top.v"])
        # threaded through mid into the leaf's new port
        self.assertIn("w_data", files["mid.v"])
        self.assertIn("sink_in", files["leaf.v"])

    def test_idempotent(self):
        self.assertEqual(run_tool(self.rtl, self.csv).returncode, 0)
        first = read_all(self.rtl)
        self.assertEqual(run_tool(self.rtl, self.csv).returncode, 0)
        second = read_all(self.rtl)
        self.assertEqual(first, second, "second run was not byte-identical")

    def test_generated_rtl_elaborates(self):
        self.assertEqual(run_tool(self.rtl, self.csv).returncode, 0)
        self.assertEqual(elaboration_errors(self.rtl), 0,
                         "generated RTL has elaboration errors")

    def test_missing_source_is_fatal(self):
        bad = self.rtl / "bad.csv"
        bad.write_text(
            "wire_name,bit_width,src,dst,comment\n"
            "w_z,1,top.does_not_exist,top/u_mid/u_leaf.sink_in,bad\n",
            newline="\n")
        r = run_tool(self.rtl, bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not found", (r.stdout + r.stderr).lower())


class AnsiE2ETests(_E2EBase, unittest.TestCase):
    make_fixture = staticmethod(write_fixture)


class NonAnsiE2ETests(_E2EBase, unittest.TestCase):
    make_fixture = staticmethod(write_fixture_nonansi)


# ── instance arrays + generate blocks ────────────────────────────────────────
def write_fixture_arraygen(d: Path):
    """A design using an instance array and a generate-for loop, plus one plain
    instance, to exercise hierarchy completeness and the write-back guard."""
    (d / "leaf.v").write_text(textwrap.dedent("""\
        module leaf (input clk, input [7:0] d);
        endmodule
    """), newline="\n")
    (d / "top.v").write_text(textwrap.dedent("""\
        module top (input clk, input [7:0] data_in);
            leaf u_arr [1:0] (.clk(clk));
            genvar i;
            generate for (i=0;i<2;i=i+1) begin : g_blk
                leaf u_gen (.clk(clk));
            end endgenerate
            leaf u_plain (.clk(clk));
        endmodule
    """), newline="\n")


class ArrayGenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rtl = Path(self.tmp)
        write_fixture_arraygen(self.rtl)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _csv(self, dst):
        p = self.rtl / "conn.csv"
        p.write_text("wire_name,bit_width,src,dst,comment\n"
                     f"w_data,8,top.data_in,{dst},x\n", newline="\n")
        return p

    def test_hierarchy_includes_array_and_generate(self):
        db = aw.RTLDatabase()
        db.scan_dir(self.rtl, "top")
        db.build_hierarchy("top")
        self.assertEqual(db.errors, [])
        self.assertIsNotNone(db.node("top/u_arr[1]"))
        self.assertIsNotNone(db.node("top/g_blk[0]/u_gen"))
        self.assertIsNotNone(db.node("top/u_plain"))

    def test_plain_endpoint_still_wires(self):
        r = run_tool(self.rtl, self._csv("top/u_plain.d"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("assign w_data = data_in", read_all(self.rtl)["top.v"])
        self.assertEqual(elaboration_errors(self.rtl), 0)
        # idempotent
        first = read_all(self.rtl)
        self.assertEqual(run_tool(self.rtl, self._csv("top/u_plain.d")).returncode, 0)
        self.assertEqual(first, read_all(self.rtl))

    def test_array_endpoint_refused(self):
        r = run_tool(self.rtl, self._csv("top/u_arr[0].d"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("instance array or generate block", (r.stdout + r.stderr))

    def test_generate_endpoint_refused(self):
        r = run_tool(self.rtl, self._csv("top/g_blk[0]/u_gen.d"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("instance array or generate block", (r.stdout + r.stderr))


# ── pyslang import failure ───────────────────────────────────────────────────
class PyslangImportTests(unittest.TestCase):
    def _run(self, *pyflags, env=None):
        return subprocess.run(
            [sys.executable, *pyflags, AUTOWIRE,
             "-d", ".", "-T", "top", "-c", "x.csv"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env,
        )

    def test_unloadable_pyslang_reports_real_cause(self):
        # installed but unloadable (e.g. DLL blocked): reinstalling won't help
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "pyslang").mkdir()
            (Path(d) / "pyslang" / "__init__.py").write_text(
                'raise ImportError("DLL load failed (simulated)")\n')
            r = self._run(env=dict(os.environ, PYTHONPATH=d))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DLL load failed (simulated)", r.stderr)
        self.assertNotIn("pip install", r.stderr)

    def test_missing_pyslang_suggests_install(self):
        r = self._run("-S", "-E")  # no site-packages: pyslang truly absent
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pip install pyslang", r.stderr)


# ── regression cases from the 2026-10 review (items 1–6) ─────────────────────
IVERILOG = shutil.which("iverilog")
VVP = shutil.which("vvp")
SELF_ASSIGN = re.compile(r"\bassign\s+(\w+)\s*=\s*\1\s*;")


def write_files(d: Path, files, newline="\n", encoding="utf-8"):
    for name, text in files.items():
        (d / name).write_text(textwrap.dedent(text), newline=newline,
                              encoding=encoding)


def write_csv(d: Path, *rows):
    p = d / "conn.csv"
    p.write_text("wire_name,bit_width,src,dst,comment\n"
                 + "".join(r + "\n" for r in rows), newline="\n")
    return p


def simulate(rtldir, tb_text):
    """Compile rtldir/*.v plus a testbench with iverilog, run it with vvp and
    return the testbench's '@'-prefixed output lines."""
    with tempfile.TemporaryDirectory() as d:
        tb = Path(d) / "tb.v"
        tb.write_text(textwrap.dedent(tb_text), newline="\n")
        exe = str(Path(d) / "sim.vvp")
        vfiles = sorted(glob.glob(os.path.join(str(rtldir), "*.v")))
        comp = subprocess.run([IVERILOG, "-o", exe, "-s", "tb", str(tb), *vfiles],
                              capture_output=True, text=True)
        if comp.returncode != 0:
            raise AssertionError(comp.stdout + comp.stderr)
        out = subprocess.run([VVP, "-n", exe], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l.startswith("@")]


class _TmpRTL(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rtl = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertClean(self):
        """Generated RTL elaborates and contains no `assign x = x;`."""
        self.assertEqual(elaboration_errors(self.rtl), 0,
                         "generated RTL has elaboration errors")
        for name, text in read_all(self.rtl).items():
            self.assertIsNone(SELF_ASSIGN.search(text), f"self-assign in {name}")

    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.rtl.glob("*.v"))}

    def assertRejected(self, csv, *needles):
        """The run fails before writing anything and mentions every needle."""
        before = self.snapshot()
        r = run_tool(self.rtl, csv)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        for needle in needles:
            self.assertIn(needle, out)
        self.assertEqual(before, self.snapshot(), "files were modified")


class DeepSourceTests(_TmpRTL):
    """Item 1: a source two levels below the LCA is routed up through mid."""
    def setUp(self):
        super().setUp()
        write_files(self.rtl, {
            "leaf.v": """\
                module leaf (input [7:0] d, output [7:0] q);
                    assign q = d;
                endmodule
            """,
            "mid.v": """\
                module mid (input [7:0] d);
                    leaf u_leaf (.d(d));
                endmodule
            """,
            "top.v": """\
                module top (input [7:0] d);
                    mid u_mid (.d(d));
                endmodule
            """})
        self.csv = write_csv(self.rtl, "w_q,8,top/u_mid/u_leaf.q,top.q_out,up two levels")

    def test_no_self_assign(self):
        r = run_tool(self.rtl, self.csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertClean()

    @unittest.skipUnless(IVERILOG and VVP, "no iverilog/vvp on PATH")
    def test_routed_value_simulates(self):
        self.assertEqual(run_tool(self.rtl, self.csv).returncode, 0)
        lines = simulate(self.rtl, """\
            module tb;
                reg  [7:0] d;
                wire [7:0] q_out;
                top dut (.d(d), .q_out(q_out));
                initial begin
                    d = 8'h00; #1 $display("@ %h %h", d, q_out);
                    d = 8'h5a; #1 $display("@ %h %h", d, q_out);
                    d = 8'hff; #1 $display("@ %h %h", d, q_out);
                end
            endmodule
        """)
        self.assertEqual(len(lines), 3, lines)
        for line in lines:
            _, sent, seen = line.split()
            self.assertEqual(seen, sent, line)


class TapWiredOutputTests(_TmpRTL):
    """Item 2: tapping a source output that is already wired must keep it."""
    LEAF = """\
        module leaf (input [7:0] d, output [7:0] q);
            assign q = d;
        endmodule
    """

    def test_existing_connection_and_load_kept(self):
        write_files(self.rtl, {
            "leaf.v": self.LEAF,
            "sink.v": """\
                module sink (input [7:0] x, output [7:0] y);
                    assign y = x;
                endmodule
            """,
            "top.v": """\
                module top (input [7:0] d, output [7:0] y);
                    wire [7:0] q_net;
                    leaf u_leaf (.d(d), .q(q_net));
                    sink u_sink (.x(q_net), .y(y));
                endmodule
            """})
        r = run_tool(self.rtl, write_csv(self.rtl, "w_tap,8,top/u_leaf.q,top.tap_out,tap"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(".q(q_net)", read_all(self.rtl)["top.v"])
        self.assertClean()
        if IVERILOG and VVP:
            lines = simulate(self.rtl, """\
                module tb;
                    reg  [7:0] d;
                    wire [7:0] y, tap_out;
                    top dut (.d(d), .y(y), .tap_out(tap_out));
                    initial begin
                        d = 8'h3c; #1 $display("@ %h %h %h", d, y, tap_out);
                        d = 8'hc3; #1 $display("@ %h %h %h", d, y, tap_out);
                    end
                endmodule
            """)
            self.assertEqual(len(lines), 2, lines)
            for line in lines:
                _, sent, load, tap = line.split()
                self.assertEqual((load, tap), (sent, sent), line)

    def test_rewritten_connection_restored_on_rerun(self):
        write_files(self.rtl, {
            "leaf.v": self.LEAF,
            "top.v": """\
                module top (input [7:0] d);
                    leaf u_leaf (.d(d), .q());
                endmodule
            """})
        r = run_tool(self.rtl, write_csv(self.rtl, "w_tap,8,top/u_leaf.q,top.tap_out,tap"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertClean()
        # Drop the tap; the new row still rewrites both top.v and leaf.v.
        r = run_tool(self.rtl, write_csv(self.rtl, "w_in,8,top.d,top/u_leaf.extra_in,other"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        files = read_all(self.rtl)
        self.assertIn(".q()", files["top.v"])
        for name, text in files.items():
            self.assertNotIn("aw-orig", text, name)
            self.assertNotIn("w_tap", text, name)
        self.assertClean()


class ParameterizedModuleTests(_TmpRTL):
    """Items 3 and 4: #(parameter) headers and #(.P(v)) instance overrides."""
    LEAF = """\
        module leaf #(parameter W = 8) (
            input          clk,
            input  [W-1:0] d
        );
        endmodule
    """
    ROW = "w_data,8,top.data_in,top/u_leaf.sink_in,new port"

    def _check(self, top_v):
        write_files(self.rtl, {"leaf.v": self.LEAF, "top.v": top_v})
        r = run_tool(self.rtl, write_csv(self.rtl, self.ROW))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        files = read_all(self.rtl)
        self.assertIn("#(parameter W = 8) (", files["leaf.v"])
        self.assertRegex(files["top.v"], r"\.sink_in\s*\(\s*w_data\s*\)")
        self.assertClean()

    def test_parameter_header_gets_port(self):
        self._check("""\
            module top (input clk, input [7:0] data_in);
                leaf u_leaf (.clk(clk), .d(data_in));
            endmodule
        """)

    def test_single_line_parameter_override(self):
        self._check("""\
            module top (input clk, input [7:0] data_in);
                leaf #(.W(8)) u_leaf (.clk(clk), .d(data_in));
            endmodule
        """)

    def test_multi_line_parameter_override(self):
        self._check("""\
            module top (input clk, input [7:0] data_in);
                leaf #(
                    .W(8)
                ) u_leaf (
                    .clk(clk),
                    .d(data_in)
                );
            endmodule
        """)

    def test_unlocatable_edit_writes_nothing(self):
        write_files(self.rtl, {
            "leaf.v": """\
                module leaf;
                endmodule
            """,
            "top.v": """\
                module top (input [7:0] data_in);
                    leaf u_leaf ();
                endmodule
            """})
        before = {p.name: p.read_bytes() for p in self.rtl.glob("*.v")}
        r = run_tool(self.rtl, write_csv(self.rtl, self.ROW))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No files written", r.stdout + r.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.rtl.glob("*.v")})


class MultiModuleFileTests(_TmpRTL):
    """Item 5: two modules in one file both receive their edits."""
    def test_both_modules_edited(self):
        write_files(self.rtl, {
            "mid_leaf.v": """\
                module leaf (
                    input clk
                );
                endmodule

                module mid (
                    input clk
                );
                    leaf u_leaf (.clk(clk));
                endmodule
            """,
            "top.v": """\
                module top (
                    input        clk,
                    input  [7:0] data_in
                );
                    mid u_mid (.clk(clk));
                endmodule
            """})
        csv = write_csv(self.rtl, "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x")
        r = run_tool(self.rtl, csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        leaf_part, mid_part = read_all(self.rtl)["mid_leaf.v"].split("module mid")
        self.assertIn("sink_in", leaf_part)
        self.assertIn("w_data", mid_part)
        self.assertClean()
        first = read_all(self.rtl)
        self.assertEqual(run_tool(self.rtl, csv).returncode, 0)
        self.assertEqual(first, read_all(self.rtl), "second run was not byte-identical")


class EncodingTests(_TmpRTL):
    """Item 6: files keep their encoding, untouched bytes and line endings."""
    COMMENT = "// 中文註解：資料輸入"

    def _run(self, encoding="utf-8", newline="\n"):
        write_fixture(self.rtl)
        for p in self.rtl.glob("*.v"):
            text = p.read_text(encoding="utf-8")
            if p.name == "leaf.v":
                text = text.replace("endmodule", self.COMMENT + "\nendmodule")
            p.write_text(text, encoding=encoding, newline=newline)
        csv = write_csv(self.rtl, "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x")
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONUTF8", None)   # the tool's default (locale) mode
            r = run_tool(self.rtl, csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = (self.rtl / "leaf.v").read_bytes()
        self.assertIn(b"sink_in", data)
        return data

    def test_utf8_comment_preserved(self):
        data = self._run("utf-8")
        self.assertIn(self.COMMENT.encode("utf-8"), data)
        data.decode("utf-8")

    def test_big5_comment_preserved(self):
        data = self._run("cp950")
        self.assertIn(self.COMMENT.encode("cp950"), data)

    def test_crlf_preserved(self):
        data = self._run(newline="\r\n")
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

    def test_mixed_eol_untouched_lines_keep_endings(self):
        write_fixture(self.rtl)
        leaf = b"module leaf (\r\n    input clk\n);\r\n// note\nendmodule\r\n"
        (self.rtl / "leaf.v").write_bytes(leaf)
        csv = write_csv(self.rtl, "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x")
        self.assertEqual(run_tool(self.rtl, csv).returncode, 0)
        self.assertIn(b"sink_in", (self.rtl / "leaf.v").read_bytes())
        self.assertEqual(run_tool(self.rtl, write_csv(self.rtl)).returncode, 0)
        self.assertEqual((self.rtl / "leaf.v").read_bytes(), leaf)


# ── regression cases from the 2026-10 review (items 7–13) ────────────────────
LEAF_Q = """\
    module leaf (input clk, output [7:0] q);
        assign q = 8'h5a;
    endmodule
"""
MID_LEAF = """\
    module mid (input clk);
        leaf u_leaf (.clk(clk));
    endmodule
"""


class NameConflictTests(_TmpRTL):
    """Item 7: a crossing-wire name must not collide with existing signals."""
    def test_wire_name_exists_in_lca(self):
        write_fixture(self.rtl)
        write_files(self.rtl, {"top.v": """\
            module top (
                input        clk,
                input  [7:0] data_in
            );
                wire [7:0] w_data = data_in;
                mid u_mid (.clk(clk));
            endmodule
        """})
        self.assertRejected(write_csv(self.rtl, "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x"),
                            "already exists")

    def test_wire_name_exists_in_intermediate(self):
        write_files(self.rtl, {
            "leaf.v": """\
                module leaf (input clk);
                endmodule
            """,
            "mid.v": """\
                module mid (input clk, input [7:0] w_data);
                    leaf u_leaf (.clk(clk));
                endmodule
            """,
            "top.v": """\
                module top (input clk, input [7:0] data_in, input [7:0] other);
                    mid u_mid (.clk(clk), .w_data(other));
                endmodule
            """})
        self.assertRejected(write_csv(self.rtl, "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x"),
                            "already exists")

    def test_wire_name_equals_lca_dst_port(self):
        write_files(self.rtl, {"leaf.v": LEAF_Q, "mid.v": MID_LEAF, "top.v": """\
            module top (input clk);
                mid u_mid (.clk(clk));
            endmodule
        """})
        self.assertRejected(write_csv(self.rtl, "busy_tap,8,top/u_mid/u_leaf.q,top.busy_tap,x"),
                            "busy_tap")

    def test_module_on_both_sides_of_route(self):
        write_files(self.rtl, {
            "leaf.v": LEAF_Q,
            "blk.v": """\
                module blk (input clk);
                    leaf u_leaf (.clk(clk));
                endmodule
            """,
            "top.v": """\
                module top (input clk);
                    blk u_a (.clk(clk));
                    blk u_b (.clk(clk));
                endmodule
            """})
        self.assertRejected(write_csv(self.rtl, "w_x,8,top/u_a/u_leaf.q,top/u_b/u_leaf.d,x"),
                            "conflicting")

    def test_wire_name_equals_parameter(self):
        write_fixture(self.rtl)
        write_files(self.rtl, {"top.v": """\
            module top (
                input        clk,
                input  [7:0] data_in
            );
                localparam w_p = 1;
                mid u_mid (.clk(clk));
            endmodule
        """})
        self.assertRejected(write_csv(self.rtl, "w_p,8,top.data_in,top/u_mid/u_leaf.sink_in,x"),
                            "already exists")

    def test_wire_name_equals_instance(self):
        write_fixture(self.rtl)
        self.assertRejected(write_csv(self.rtl, "u_mid,8,top.data_in,top/u_mid/u_leaf.sink_in,x"),
                            "already exists")

    def test_fan_out_from_unconnected_output(self):
        write_files(self.rtl, {
            "leaf.v": """\
                module leaf (input [7:0] d, output [7:0] q);
                    assign q = d;
                endmodule
            """,
            "sink.v": """\
                module sink (input [7:0] x, output [7:0] y);
                    assign y = x;
                endmodule
            """,
            "top.v": """\
                module top (input [7:0] d, output [7:0] y1, output [7:0] y2);
                    leaf u_leaf (.d(d));
                    sink u_s1 (.y(y1));
                    sink u_s2 (.y(y2));
                endmodule
            """})
        r = run_tool(self.rtl, write_csv(self.rtl, "w_a,8,top/u_leaf.q,top/u_s1.x,first",
                                         "w_b,8,top/u_leaf.q,top/u_s2.x,second"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(read_all(self.rtl)["leaf.v"], r"assign\s+w_b\s*=\s*q\s*;")
        self.assertClean()
        if IVERILOG and VVP:
            lines = simulate(self.rtl, """\
                module tb;
                    reg  [7:0] d;
                    wire [7:0] y1, y2;
                    top dut (.d(d), .y1(y1), .y2(y2));
                    initial begin
                        d = 8'h69; #1 $display("@ %h %h %h", d, y1, y2);
                    end
                endmodule
            """)
            self.assertEqual(lines, ["@ 69 69 69"])


class DestinationTests(_TmpRTL):
    """Items 8 and 9: what may be a destination, and only once."""
    TOP = """\
        module top (input clk, input [7:0] data_in, input [7:0] other);
            mid u_mid (.clk(clk));
        endmodule
    """

    def test_dst_output_rejected(self):
        write_files(self.rtl, {"leaf.v": LEAF_Q, "mid.v": MID_LEAF, "top.v": self.TOP})
        self.assertRejected(write_csv(self.rtl, "w_d,8,top.data_in,top/u_mid/u_leaf.q,x"),
                            "output")

    def test_lca_scope_input_rejected(self):
        write_files(self.rtl, {"leaf.v": LEAF_Q, "mid.v": MID_LEAF, "top.v": self.TOP})
        self.assertRejected(write_csv(self.rtl, "w_q,8,top/u_mid/u_leaf.q,top.other,x"),
                            "input")

    def test_dst_typo_warns_with_similar_port(self):
        write_files(self.rtl, {"leaf.v": """\
            module leaf (input clk, input [7:0] data_i);
            endmodule
        """, "mid.v": MID_LEAF, "top.v": self.TOP})
        r = run_tool(self.rtl, write_csv(self.rtl, "w_d,8,top.data_in,top/u_mid/u_leaf.data_in,x"),
                     answer="n\n")
        out = r.stdout + r.stderr
        self.assertIn('dst port "data_in"', out)
        self.assertIn("similar port: data_i", out)

    def test_new_dst_without_similar_port_is_note(self):
        write_files(self.rtl, {"leaf.v": LEAF_Q, "mid.v": MID_LEAF, "top.v": self.TOP})
        r = run_tool(self.rtl, write_csv(self.rtl, "w_d,8,top.data_in,top/u_mid/u_leaf.sink_in,x"),
                     answer="n\n")
        out = r.stdout + r.stderr
        self.assertIn('NOTE row 2: dst port "sink_in"', out)
        self.assertNotIn("similar port", out)

    def test_dst_internal_signal_rejected(self):
        write_files(self.rtl, {"leaf.v": """\
            module leaf (input clk);
                wire [7:0] sink_in;
            endmodule
        """, "mid.v": MID_LEAF, "top.v": self.TOP})
        self.assertRejected(write_csv(self.rtl, "w_d,8,top.data_in,top/u_mid/u_leaf.sink_in,x"),
                            "not a port")

    def test_connected_dst_warns_and_rewires(self):
        write_files(self.rtl, {
            "leaf.v": """\
                module leaf (input clk, input [7:0] d);
                endmodule
            """,
            "mid.v": """\
                module mid (input clk, input [7:0] old);
                    leaf u_leaf (.clk(clk), .d(old));
                endmodule
            """,
            "top.v": """\
                module top (input clk, input [7:0] data_in, input [7:0] other);
                    mid u_mid (.clk(clk), .old(other));
                endmodule
            """})
        r = run_tool(self.rtl, write_csv(self.rtl, "w_d,8,top.data_in,top/u_mid/u_leaf.d,x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already connected", r.stdout)
        self.assertClean()

    def test_rewire_in_shared_module_rejected(self):
        # mid is instantiated twice: rewiring u_leaf.d inside it would change both.
        write_files(self.rtl, {
            "leaf.v": """\
                module leaf (input clk, input [7:0] d);
                endmodule
            """,
            "mid.v": """\
                module mid (input clk, input [7:0] old);
                    leaf u_leaf (.clk(clk), .d(old));
                endmodule
            """,
            "top.v": """\
                module top (input clk, input [7:0] data_in, input [7:0] other);
                    mid u_mid (.clk(clk), .old(other));
                    mid u_mid2 (.clk(clk), .old(other));
                endmodule
            """})
        self.assertRejected(write_csv(self.rtl, "w_d,8,top.data_in,top/u_mid/u_leaf.d,x"),
                            "instantiated 2 times")

    def test_duplicate_dst_rejected(self):
        write_fixture(self.rtl)
        self.assertRejected(write_csv(self.rtl,
                                      "w_a,8,top.data_in,top/u_mid/u_leaf.sink_in,a",
                                      "w_b,8,top.data_in,top/u_mid/u_leaf.sink_in,b"),
                            "Row 3", "row 2")


class SameScopeTests(_TmpRTL):
    """Item 10: src and dst in the same module."""
    def test_same_scope_creates_driven_output(self):
        write_fixture(self.rtl)
        r = run_tool(self.rtl, write_csv(self.rtl, "w_dbg,8,top.data_in,top.dbg_out,x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(read_all(self.rtl)["top.v"], r"output\s+wire\s+\[7:0\]\s+dbg_out")
        self.assertClean()
        if IVERILOG and VVP:
            lines = simulate(self.rtl, """\
                module tb;
                    reg clk = 0; reg [7:0] data_in;
                    wire [7:0] dbg_out;
                    top dut (.clk(clk), .data_in(data_in), .dbg_out(dbg_out));
                    initial begin
                        data_in = 8'h3c; #1 $display("@ %h %h", data_in, dbg_out);
                    end
                endmodule
            """)
            self.assertEqual(lines, ["@ 3c 3c"])

    def test_same_endpoint_rejected(self):
        write_fixture(self.rtl)
        self.assertRejected(write_csv(self.rtl, "w_x,8,top.data_in,top.data_in,x"),
                            "same signal")


class AdapterPlacementTests(_TmpRTL):
    """Item 11: a width adapter for a dst in the LCA's own scope."""
    def test_adapter_in_lca_scope(self):
        write_files(self.rtl, {
            "leaf.v": """\
                module leaf (input clk, output [7:0] q);
                    assign q = 8'ha5;
                endmodule
            """,
            "mid.v": """\
                module mid (input clk, output [15:0] y16);
                    leaf u_leaf (.clk(clk));
                endmodule
            """,
            "top.v": """\
                module top (input clk, output [15:0] y16);
                    mid u_mid (.clk(clk), .y16(y16));
                endmodule
            """})
        r = run_tool(self.rtl, write_csv(self.rtl, "w_q,8,top/u_mid/u_leaf.q,top/u_mid.y16,8b to 16b"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertClean()
        if IVERILOG and VVP:
            lines = simulate(self.rtl, """\
                module tb;
                    reg clk = 0;
                    wire [15:0] y16;
                    top dut (.clk(clk), .y16(y16));
                    initial #1 $display("@ %h", y16);
                endmodule
            """)
            self.assertEqual(lines, ["@ 00a5"])


class ReconcileTests(_TmpRTL):
    """Item 12: every run makes the scanned RTL match the CSV."""
    ROW = "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x"

    def test_removed_row_leaves_no_residue(self):
        write_fixture(self.rtl)
        write_files(self.rtl, {
            "sub.v": """\
                module sub (input clk);
                endmodule
            """,
            "side.v": """\
                module side (input clk, input [3:0] s);
                    sub u_sub (.clk(clk));
                endmodule
            """,
            "top.v": """\
                module top (
                    input        clk,
                    input  [7:0] data_in,
                    input  [3:0] s
                );
                    mid u_mid (.clk(clk));
                    side u_side (.clk(clk), .s(s));
                endmodule
            """})
        original = self.snapshot()
        self.assertEqual(run_tool(self.rtl, write_csv(self.rtl, self.ROW)).returncode, 0)
        # The new CSV only touches side.v and sub.v.
        r = run_tool(self.rtl, write_csv(self.rtl, "w_s,4,top/u_side.s,top/u_side/u_sub.s_in,x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        now = self.snapshot()
        for name in ("top.v", "mid.v", "leaf.v"):
            self.assertEqual(now[name], original[name], name)
        self.assertClean()

    def test_empty_csv_removes_all_tool_content(self):
        write_fixture(self.rtl)
        original = self.snapshot()
        self.assertEqual(run_tool(self.rtl, write_csv(self.rtl, self.ROW)).returncode, 0)
        self.assertNotEqual(original, self.snapshot())
        r = run_tool(self.rtl, write_csv(self.rtl))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(original, self.snapshot())

    def test_absolute_rtl_dir_rerun_is_idempotent(self):
        # With the cwd on the same drive, pyslang reports file paths relative
        # to it; they must still map to the files scan_dir keyed by -d.
        write_fixture(self.rtl)
        csv = write_csv(self.rtl, self.ROW)
        self.assertEqual(run_tool(self.rtl, csv, cwd=self.rtl).returncode, 0)
        first = self.snapshot()
        self.assertIn(b"sink_in", first["leaf.v"])
        r = run_tool(self.rtl, csv, cwd=self.rtl)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(first, self.snapshot(), "second run changed the files")

    def test_cleanup_keeps_blank_lines(self):
        write_files(self.rtl, {
            "leaf.v": """\
                module leaf (
                    input clk
                );

                endmodule
            """,
            "mid.v": """\
                module mid (
                    input clk
                );

                    leaf u_leaf (.clk(clk));
                endmodule
            """,
            "top.v": """\
                module top (
                    input        clk,
                    input  [7:0] data_in
                );

                    mid u_mid (.clk(clk));
                endmodule
            """})
        original = self.snapshot()
        self.assertEqual(run_tool(self.rtl, write_csv(self.rtl, self.ROW)).returncode, 0)
        self.assertEqual(run_tool(self.rtl, write_csv(self.rtl)).returncode, 0)
        self.assertEqual(original, self.snapshot())

    def test_empty_csv_on_clean_tree_is_noop(self):        # guard
        write_fixture(self.rtl)
        original = self.snapshot()
        r = run_tool(self.rtl, write_csv(self.rtl))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Nothing to do", r.stdout)
        self.assertEqual(original, self.snapshot())


class ConnectionTailTests(_TmpRTL):
    """What follows an instance's last connection, e.g. a `);` on its own line
    or a trailing comment, stays after the connections the tool adds."""
    ROW = "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x"

    def _run_and_clean(self):
        original = self.snapshot()
        r = run_tool(self.rtl, write_csv(self.rtl, self.ROW))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertClean()
        r = run_tool(self.rtl, write_csv(self.rtl))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(original, self.snapshot())

    def test_own_line_close_paren_restored(self):
        write_fixture(self.rtl)
        write_files(self.rtl, {
            "mid.v": """\
                module mid (
                    input clk
                );
                    leaf u_leaf (
                        .clk (clk)
                    );
                endmodule
            """,
            "top.v": """\
                module top (
                    input        clk,
                    input  [7:0] data_in
                );
                    mid u_mid (
                        .clk (clk)
                    );
                endmodule
            """})
        self._run_and_clean()

    def test_trailing_comment_after_last_connection(self):
        write_fixture(self.rtl)
        write_files(self.rtl, {"mid.v": """\
            module mid (
                input clk
            );
                leaf u_leaf (
                    .clk (clk)   // the clock
                );
            endmodule
        """})
        self._run_and_clean()


class _ConnListBase(_TmpRTL):
    """Routes top.data_in to sink_in of u_leaf, whose connection list in mid.v
    is given line by line. With rewire, sink_in already exists and is wired
    to real_sig, so the tool rewrites that connection. body holds lines of
    mid before the instance; prefix is text before `module mid`."""
    ROW = "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x"

    def _design(self, *conns, rewire=False, body=(), prefix=""):
        write_fixture(self.rtl)
        if rewire:                           # sink_in exists, wired to real_sig
            write_files(self.rtl, {"leaf.v": """\
                module leaf (
                    input       clk,
                    input [7:0] sink_in
                );
                endmodule
            """})
        (self.rtl / "mid.v").write_text(
            prefix + "module mid (\n    input clk\n);\n    wire [7:0] real_sig;\n"
            + "".join(f"    {b}\n" for b in body)
            + "    leaf u_leaf (\n" + "".join(f"        {c}\n" for c in conns)
            + "    );\nendmodule\n", newline="\n")

    def _check(self, *conns, rewire=False, **design):
        self._design(*conns, rewire=rewire, **design)
        original = self.snapshot()
        r = run_tool(self.rtl, write_csv(self.rtl, self.ROW))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertClean()
        mid = read_all(self.rtl)["mid.v"]
        self.assertRegex(aw._mask_comments(mid), r"\.sink_in\s*\(\s*w_data\s*\)")
        # Only a real connection is rewritten, never a comment.
        self.assertEqual(mid.count(aw._ORIG_OPEN), int(rewire), mid)
        r = run_tool(self.rtl, write_csv(self.rtl))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(original, self.snapshot())


class CommentedConnectionTests(_ConnListBase):
    """A `.port(...)` inside a comment is not a connection: the tool neither
    counts nor rewrites it, and a `)` inside a comment doesn't end one."""

    def test_commented_out_connection_mid_list(self):
        self._check("// .sink_in (old)", ".clk (clk)")

    def test_commented_out_connection_last(self):          # guard
        self._check(".clk (clk)", "// .sink_in (old)")

    def test_block_commented_connection(self):
        self._check("/* .sink_in (old) */", ".clk (clk)")

    def test_rewire_leaves_commented_copy(self):
        self._check(".sink_in (real_sig),   // was .sink_in (old)", ".clk (clk)",
                    rewire=True)

    def test_paren_in_comment_inside_connection(self):
        self._check(".sink_in (real_sig  // see f(x)", "),", ".clk (clk)",
                    rewire=True)


class NestedParenTests(_ConnListBase):
    """A rewired connection keeps its whole expression, nested parentheses
    included, so the cleanup restores it exactly."""

    def test_nested_expression(self):
        self._check(".sink_in (real_sig & (real_sig | 8'h0f)),", ".clk (clk)",
                    rewire=True)

    def test_function_call(self):
        self._check(".sink_in ($unsigned(real_sig)),", ".clk (clk)", rewire=True)

    def test_doubly_nested(self):
        self._check(".sink_in ((real_sig)),", ".clk (clk)", rewire=True)

    def test_nested_expression_with_comment(self):
        self._check(".sink_in ((real_sig)  // keep (x)", "),", ".clk (clk)",
                    rewire=True)

    def test_nested_parens_in_another_connection(self):    # guard
        self._check(".clk ((clk))")

    def test_block_comment_rewire_refused(self):           # guard
        self._design(".sink_in (real_sig /* ) */ ),", ".clk (clk)", rewire=True)
        self.assertRejected(write_csv(self.rtl, self.ROW), "WRITE-BACK ERROR")


class StringLiteralTests(_ConnListBase):
    """Nothing inside a string literal is taken for code: no parenthesis,
    comment opener, `module`, `endmodule` or instance name. (A string on
    the 1-bit clk input is only truncated.)"""

    def test_open_paren_in_rewired_string(self):
        self._check(".clk (clk),", '.sink_in ("(")', rewire=True)

    def test_close_paren_in_rewired_string(self):
        self._check(".clk (clk),", '.sink_in ("a)")', rewire=True)

    def test_paren_in_string_beside_new_connection(self):
        self._check('.clk ("(")')

    def test_slashes_in_string(self):
        self._check('.clk ("a//b")')

    def test_block_comment_opener_in_string(self):
        self._check('.clk ("/*")')

    def test_module_name_in_string(self):
        self._check(".clk (clk)", prefix='module helper;\n'
                    '    initial $display("module mid");\nendmodule\n\n')

    def test_endmodule_in_string(self):
        self._check(".clk (clk)", body=['initial $display("endmodule");'])

    def test_instance_in_string(self):
        self._check(".clk (clk)", body=['initial $display("leaf u_leaf (");'])

    def test_plain_string(self):                           # guard
        self._check('.clk ("ab")')


class PromptTests(_TmpRTL):
    """Item 13: only Enter / y / yes proceeds."""
    ROW = "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x"

    def test_no_aborts(self):
        write_fixture(self.rtl)
        original = self.snapshot()
        r = run_tool(self.rtl, write_csv(self.rtl, self.ROW), answer="no\n")
        self.assertIn("Aborted", r.stdout)
        self.assertEqual(original, self.snapshot())

    def test_yes_proceeds(self):                           # guard
        write_fixture(self.rtl)
        r = run_tool(self.rtl, write_csv(self.rtl, self.ROW), answer="yes\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sink_in", read_all(self.rtl)["leaf.v"])


# ── regression cases from the 2026-10 review (remaining items) ───────────────
ROW_DOWN = "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,x"


class StampTests(_TmpRTL):
    """Re-running as another user or on another day rewrites nothing."""
    def _as(self, user, csv):
        names = {k: user for k in ("LOGNAME", "USER", "LNAME", "USERNAME")}
        with mock.patch.dict(os.environ, names):
            return run_tool(self.rtl, csv)

    def test_other_user_rerun_writes_nothing(self):
        write_fixture(self.rtl)
        csv = write_csv(self.rtl, ROW_DOWN)
        self.assertEqual(self._as("alice", csv).returncode, 0)
        first = self.snapshot()
        r = self._as("bob", csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 file(s) written", r.stdout)
        self.assertEqual(first, self.snapshot())

    def test_next_day_rerun_writes_nothing(self):
        write_fixture(self.rtl)
        csv = write_csv(self.rtl, ROW_DOWN)
        self.assertEqual(run_tool(self.rtl, csv).returncode, 0)
        first = self.snapshot()
        wrapper = self.rtl / "tomorrow.py"
        wrapper.write_text(textwrap.dedent(f"""\
            import sys, datetime
            sys.path.insert(0, {str(ROOT)!r})
            import autowire_v3 as aw
            class D(datetime.date):
                @classmethod
                def today(cls):
                    return datetime.date.today() + datetime.timedelta(days=1)
            aw.dt_date = D
            sys.argv = ["autowire_v3.py"] + sys.argv[1:]
            aw.main()
        """), newline="\n")
        r = subprocess.run(
            [sys.executable, str(wrapper), "-d", str(self.rtl), "-T", "top",
             "-c", str(csv), "--no-color", "--out-log", str(self.rtl / "log.txt")],
            input="y\n", capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(first, self.snapshot())


class CsvTests(_TmpRTL):
    """The connections CSV is decoded like the RTL (BOM, UTF-8, locale)."""
    ROWS = ("wire_name,bit_width,src,dst,comment\n"
            "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,中文說明\n")

    def _run(self, data: bytes):
        write_fixture(self.rtl)
        csv = self.rtl / "conn.csv"
        csv.write_bytes(data)
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONUTF8", None)
            r = run_tool(self.rtl, csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sink_in", read_all(self.rtl)["leaf.v"])

    def test_bom_csv_with_comment_first_line(self):
        self._run(("# exported from Excel\n" + self.ROWS).encode("utf-8-sig"))

    def test_cp950_csv(self):                              # guard
        self._run(self.ROWS.encode("cp950"))


class CliTests(_TmpRTL):
    def test_dry_run_writes_nothing(self):
        write_fixture(self.rtl)
        original = self.snapshot()
        r = run_tool(self.rtl, write_csv(self.rtl, ROW_DOWN), extra=("--dry-run",))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.rtl / "autowire_warn.log").exists())
        self.assertEqual(original, self.snapshot())

    def test_new_dst_port_is_a_note(self):
        write_fixture(self.rtl)
        r = run_tool(self.rtl, write_csv(self.rtl, ROW_DOWN))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("may be external IP", r.stdout)
        self.assertIn("will be added", r.stdout)

    def test_tree_without_csv(self):
        write_fixture(self.rtl)
        r = subprocess.run(
            [sys.executable, AUTOWIRE, "-d", str(self.rtl), "-T", "top", "--tree",
             "--no-color"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Hierarchy tree", r.stdout)


class SvTests(_TmpRTL):
    """SystemVerilog sources: *.sv files and implicit `.name` connections."""
    def test_sv_file_is_scanned_and_edited(self):
        write_fixture(self.rtl)
        (self.rtl / "leaf.v").unlink()
        write_files(self.rtl, {"leaf.sv": """\
            module leaf (
                input logic clk
            );
            endmodule
        """})
        r = run_tool(self.rtl, write_csv(self.rtl, ROW_DOWN))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sink_in", (self.rtl / "leaf.sv").read_text())
        self.assertEqual(elaboration_errors(self.rtl), 0)

    def test_implicit_named_connection_is_wired(self):
        write_files(self.rtl, {
            "leaf.sv": """\
                module leaf (input logic clk, input logic [7:0] d, output logic [7:0] q);
                    assign q = d;
                endmodule
            """,
            "top.sv": """\
                module top (input logic clk, input logic [7:0] d);
                    logic [7:0] q;
                    leaf u_leaf (.clk, .d, .q);
                endmodule
            """})
        r = run_tool(self.rtl, write_csv(self.rtl, "w_tap,8,top/u_leaf.q,top.q_out,tap"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex((self.rtl / "top.sv").read_text(), r"\(\.clk, \.d, \.q[,)]")
        self.assertEqual(elaboration_errors(self.rtl), 0)


class IncludeTests(_TmpRTL):
    """`include'd files, with the absolute --rtl-dir run_tool always passes."""
    def _macro_design(self, header):
        write_fixture(self.rtl)
        write_files(self.rtl, {
            header: """\
                `define DATA_W 8
            """,
            "top.v": f"""\
                `include "{header}"
                module top (
                    input                clk,
                    input  [`DATA_W-1:0] data_in
                );
                    mid u_mid (.clk(clk));
                endmodule
            """})
        return write_csv(self.rtl, ROW_DOWN)

    def _module_design(self, name="sub.v"):
        write_fixture(self.rtl)
        write_files(self.rtl, {
            name: """\
                module sub (input clk);
                endmodule
            """,
            "top.v": f"""\
                `include "{name}"
                module top (
                    input        clk,
                    input  [7:0] data_in
                );
                    mid u_mid (.clk(clk));
                    sub u_sub (.clk(clk));
                endmodule
            """})

    def _check_reruns(self, csv):
        r = run_tool(self.rtl, csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("elaboration error", r.stdout)
        first = self.snapshot()
        r = run_tool(self.rtl, csv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 file(s) written", r.stdout)
        self.assertEqual(first, self.snapshot())

    def test_macro_header_sorted_after_includer(self):
        self._check_reruns(self._macro_design("top_defines.v"))

    def test_macro_header_sorted_before_includer(self):
        self._check_reruns(self._macro_design("a_defines.v"))

    def test_included_module_file_not_edited(self):
        self._module_design()
        self._check_reruns(write_csv(self.rtl, ROW_DOWN))

    def test_included_module_file_can_be_edited(self):
        # The include must see sub.v without the tool's earlier output, or the
        # rerun would collide with the ports added by the first run.
        self._module_design()
        self._check_reruns(write_csv(self.rtl, "w_s,8,top.data_in,top/u_sub.s_in,x"))
        self.assertIn("s_in", read_all(self.rtl)["sub.v"])

    def test_include_in_inactive_ifdef_is_still_compiled(self):
        write_fixture(self.rtl)
        write_files(self.rtl, {
            "sub.v": """\
                module sub (input clk);
                endmodule
            """,
            "top.v": """\
                `ifdef NEVER_DEFINED
                `include "sub.v"
                `endif
                module top (
                    input        clk,
                    input  [7:0] data_in
                );
                    mid u_mid (.clk(clk));
                    sub u_sub (.clk(clk));
                endmodule
            """})
        self._check_reruns(write_csv(self.rtl, "w_s,8,top.data_in,top/u_sub.s_in,x"))

    def _check_header_module(self, header):
        # A header isn't compiled on its own, but a module in it is edited like
        # any other: the include must see it without the tool's earlier output.
        self._module_design(header)
        original, pristine = self.snapshot(), (self.rtl / header).read_bytes()
        self._check_reruns(write_csv(self.rtl, "w_s,8,top.data_in,top/u_sub.s_in,x"))
        self.assertIn(b"s_in", (self.rtl / header).read_bytes())
        r = run_tool(self.rtl, write_csv(self.rtl))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(original, self.snapshot())
        self.assertEqual(pristine, (self.rtl / header).read_bytes())

    def test_module_in_vh_header_can_be_edited(self):
        self._check_header_module("sub.vh")

    def test_module_in_svh_header_can_be_edited(self):
        self._check_header_module("sub.svh")


class SampleTests(_TmpRTL):
    """The bundled i2c sample with the bundled wire_connect.csv."""
    def test_bundled_sample_with_wire_connect_csv(self):
        shutil.rmtree(self.tmp)
        shutil.copytree(ROOT / "test_rtl" / "i2c-master", self.tmp)
        csv = ROOT / "wire_connect.csv"
        r = run_tool(self.rtl, csv, top="i2c_master_top")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("elaboration error", r.stdout)
        self.assertIn("test_c", read_all(self.rtl)["i2c_master_bit_ctrl.v"])
        first = self.snapshot()
        r = run_tool(self.rtl, csv, top="i2c_master_top")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(first, self.snapshot())


# ── machine interface: --yes, --json, --expect-plan ──────────────────────────
class MachineInterfaceTests(_TmpRTL):
    ROW = "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,route down"

    def setUp(self):
        super().setUp()
        write_fixture(self.rtl)
        self.csv = write_csv(self.rtl, self.ROW)
        self.json = self.rtl / "out.json"

    def run_json(self, *extra, answer=""):
        r = run_tool(self.rtl, self.csv, extra=("--json", str(self.json), *extra),
                     answer=answer)
        rep = json.loads(self.json.read_text(encoding="utf-8")) if self.json.exists() else None
        return r, rep

    def test_yes_writes_without_input_same_as_answering_y(self):
        r = run_tool(self.rtl, self.csv, extra=("--yes",), answer="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Proceed with write-back", r.stdout)
        with_yes = self.snapshot()
        other = Path(tempfile.mkdtemp())
        try:
            write_fixture(other)
            run_tool(other, write_csv(other, self.ROW), answer="y\n")
            self.assertEqual(with_yes, {p.name: p.read_bytes()
                                        for p in sorted(other.glob("*.v"))})
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_no_input_without_yes_writes_nothing(self):
        before = self.snapshot()
        r = run_tool(self.rtl, self.csv, answer="")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(before, self.snapshot())

    def test_preview_json_matches_what_is_written(self):
        before = self.snapshot()
        r, rep = self.run_json("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(before, self.snapshot(), "dry run modified files")
        self.assertEqual(rep["status"], "preview")
        self.assertEqual(rep["written"], [])
        self.assertEqual(rep["connections"][0]["wire_name"], "w_data")
        self.assertEqual(rep["connections"][0]["dst"], "top/u_mid/u_leaf.sink_in")
        self.assertTrue(any("sink_in" in w for w in rep["warnings"]))
        self.assertEqual(sorted(Path(f["path"]).name for f in rep["files"]),
                         ["leaf.v", "mid.v", "top.v"])

        r2, rep2 = self.run_json("--yes", "--expect-plan", rep["plan_id"])
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(rep2["status"], "written")
        self.assertEqual(rep2["plan_id"], rep["plan_id"])
        self.assertEqual(sorted(rep2["written"]), sorted(f["path"] for f in rep["files"]))
        after = self.snapshot()
        for f in rep["files"]:
            name = Path(f["path"]).name
            added = [l[1:] for l in f["diff"].splitlines()
                     if l.startswith("+") and not l.startswith("+++")]
            removed = [l[1:] for l in f["diff"].splitlines()
                       if l.startswith("-") and not l.startswith("---")]
            old, new = before[name].decode().splitlines(), after[name].decode().splitlines()
            for line in added:
                self.assertIn(line, new, name)
            for line in removed:
                self.assertIn(line, old, name)
            self.assertEqual(len(new) - len(old), len(added) - len(removed), name)

    def test_rtl_edit_after_preview_refuses_write(self):
        _, rep = self.run_json("--dry-run")
        top = self.rtl / "top.v"
        top.write_text(top.read_text().replace("input        clk,", "input        clk,  // edited"),
                       newline="\n")
        before = self.snapshot()
        r, rep2 = self.run_json("--yes", "--expect-plan", rep["plan_id"])
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("PLAN CHANGED", r.stdout)
        self.assertEqual(rep2["status"], "stale")
        self.assertEqual(before, self.snapshot(), "files were modified")

    def test_csv_edit_after_preview_refuses_write(self):
        _, rep = self.run_json("--dry-run")
        write_csv(self.rtl, self.ROW.replace("w_data", "w_other"))
        before = self.snapshot()
        r, _ = self.run_json("--yes", "--expect-plan", rep["plan_id"])
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(before, self.snapshot(), "files were modified")

    def test_unrelated_comment_edit_keeps_plan(self):
        write_files(self.rtl, {"spare.v": "module spare (input a);\nendmodule\n"})
        _, rep = self.run_json("--dry-run")
        (self.rtl / "spare.v").write_text("module spare (input a); // note\nendmodule\n",
                                          newline="\n")
        r, rep2 = self.run_json("--yes", "--expect-plan", rep["plan_id"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rep2["status"], "written")

    def test_rerun_after_write_reports_no_file_changes(self):
        self.run_json("--yes")
        before = self.snapshot()
        r, rep = self.run_json("--dry-run")
        self.assertEqual(rep["files"], [])
        self.assertEqual(before, self.snapshot())

    def test_edit_during_run_refuses_write(self):
        """A file changed between the scan and the write is never overwritten."""
        top = self.rtl / "top.v"

        def edit_then_yes(prompt=""):
            top.write_text(top.read_text() + "// edited meanwhile\n", newline="\n")
            return "y"

        argv = ["autowire_v3.py", "-d", str(self.rtl), "-T", "top", "-c", str(self.csv),
                "--no-color", "--out-log", str(self.rtl / "w.log")]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("builtins.input", edit_then_yes), \
             mock.patch("sys.stdout", new=io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                aw.main()
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("// edited meanwhile", top.read_text())
        self.assertNotIn("AUTO_WIRE", "".join(read_all(self.rtl).values()))

    def test_tree_json(self):
        r = run_tool(self.rtl, self.csv, extra=("--tree", "--json", str(self.json)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = json.loads(self.json.read_text(encoding="utf-8"))
        self.assertEqual(rep["status"], "tree")
        t = rep["tree"]
        self.assertEqual((t["module"], t["path"]), ("top", "top"))
        self.assertEqual(t["children"][0]["path"], "top/u_mid")
        self.assertEqual(t["children"][0]["children"][0]["path"], "top/u_mid/u_leaf")
        self.assertIn({"name": "data_in", "direction": "input", "width": 8},
                      rep["modules"]["top"]["ports"])

    def test_error_run_removes_earlier_json(self):
        self.run_json("--dry-run")
        self.assertTrue(self.json.exists())
        write_csv(self.rtl, "w_z,1,top.does_not_exist,top/u_mid/u_leaf.sink_in,bad")
        r, rep = self.run_json("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIsNone(rep)

    def test_no_json_flag_writes_no_json(self):
        r = run_tool(self.rtl, self.csv)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.json.exists())


# ── atomic write-back ────────────────────────────────────────────────────────
class AtomicWriteTests(_TmpRTL):
    """The fixture's one connection rewrites leaf.v, mid.v and top.v."""

    def setUp(self):
        super().setUp()
        write_fixture(self.rtl)
        self.csv = write_csv(self.rtl, "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,route down")
        self.json = self.rtl / "out.json"
        self.before = self.snapshot()

    def run_main(self, *patches):
        """Run main() in-process with --yes under the given mock patches.
        Returns (exit status, stdout text)."""
        argv = ["autowire_v3.py", "-d", str(self.rtl), "-T", "top", "-c", str(self.csv),
                "--no-color", "--yes", "--json", str(self.json),
                "--out-log", str(self.rtl / "w.log")]
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch("sys.stdout", new=out))
            for p in patches:
                stack.enter_context(p)
            try:
                aw.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    def temps(self):
        return sorted(p.name for p in self.rtl.rglob("*.awtmp"))

    def report(self):
        return json.loads(self.json.read_text(encoding="utf-8")) if self.json.exists() else None

    def failing_on_call(self, real, n, exc):
        calls = []

        def fake(*a, **kw):
            calls.append(a)
            if len(calls) == n:
                raise exc
            return real(*a, **kw)
        return fake

    def test_normal_run_leaves_no_temp_files(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.temps(), [])
        self.assertEqual(self.report()["status"], "written")
        self.assertEqual(len(self.report()["written"]), 3)
        self.assertNotEqual(self.before, self.snapshot())

    def test_staging_failure_writes_nothing(self):
        fake = self.failing_on_call(os.fsync, 2, OSError(28, "No space left on device"))
        code, out = self.run_main(mock.patch("os.fsync", fake))
        self.assertEqual(code, 1, out)
        self.assertIn("WRITE ERROR", out)
        self.assertIn("No files written.", out)
        self.assertEqual(self.before, self.snapshot())
        self.assertEqual(self.temps(), [])
        self.assertIsNone(self.report())
        self.assertFalse((self.rtl / "w.log").exists())

    def test_partial_commit_is_reported_and_leaves_whole_files(self):
        fake = self.failing_on_call(os.replace, 2, OSError(5, "Input/output error"))
        code, out = self.run_main(mock.patch("os.replace", fake))
        self.assertEqual(code, 1, out)
        self.assertIn("Already replaced:", out)
        self.assertEqual(self.report()["incomplete"], [])
        rep = self.report()
        self.assertEqual(rep["status"], "partial")
        self.assertEqual(len(rep["written"]), 1)
        done = Path(rep["written"][0]).name
        after = self.snapshot()
        for name in self.before:
            if name == done:
                self.assertNotEqual(after[name], self.before[name], name)
                self.assertIn(b"AUTO_WIRE_END", after[name])       # complete new content
            else:
                self.assertEqual(after[name], self.before[name], name)
        self.assertEqual(self.temps(), [])
        self.assertFalse((self.rtl / "w.log").exists())

    def test_interrupt_during_commit_is_reported(self):
        fake = self.failing_on_call(os.replace, 2, KeyboardInterrupt())
        code, out = self.run_main(mock.patch("os.replace", fake))
        self.assertEqual(code, 1, out)
        self.assertIn("interrupted", out)
        self.assertEqual(self.report()["status"], "partial")
        self.assertEqual(self.temps(), [])

    def test_interrupt_right_after_a_replace_still_reports_that_file(self):
        real, calls = os.replace, []

        def replace_then_interrupt(src, dst):
            real(src, dst)
            calls.append(dst)
            if len(calls) == 2:
                raise KeyboardInterrupt()
        code, out = self.run_main(mock.patch("os.replace", replace_then_interrupt))
        self.assertEqual(code, 1, out)
        rep = self.report()
        self.assertEqual(len(rep["written"]), 2)
        after = self.snapshot()
        changed = sorted(n for n in after if after[n] != self.before[n])
        self.assertEqual(changed, sorted(Path(p).name for p in rep["written"]))
        self.assertEqual(self.temps(), [])

    def test_unexpected_error_during_commit_is_reported_not_raised(self):
        fake = self.failing_on_call(os.replace, 2, RuntimeError("boom"))
        code, out = self.run_main(mock.patch("os.replace", fake))
        self.assertEqual(code, 1, out)
        self.assertIn("RuntimeError", out)
        self.assertEqual(len(self.report()["written"]), 1)
        self.assertEqual(self.temps(), [])

    def test_replace_refused_falls_back_to_in_place(self):
        """E.g. Windows refuses to replace a file another program has open."""
        code, out = self.run_main(
            mock.patch("os.replace", side_effect=PermissionError(13, "Permission denied")))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.report()["status"], "written")
        self.assertEqual(self.temps(), [])
        for name, data in self.snapshot().items():
            self.assertIn(b"AUTO_WIRE_END", data, name)

    def test_cut_short_in_place_write_is_reported_as_incomplete(self):
        real, calls = Path.write_bytes, []

        def half_write(path, data):
            if path.suffix == ".v":
                calls.append(path)
                if len(calls) == 2:
                    real(path, data[:len(data) // 2])
                    raise OSError(28, "No space left on device")
            return real(path, data)
        code, out = self.run_main(
            mock.patch("tempfile.mkstemp", side_effect=PermissionError(13, "Permission denied")),
            mock.patch.object(Path, "write_bytes", half_write))
        self.assertEqual(code, 1, out)
        self.assertIn("May be incomplete", out)
        rep = self.report()
        self.assertEqual(rep["status"], "partial")
        self.assertEqual(len(rep["written"]), 1)
        self.assertEqual(len(rep["incomplete"]), 1)
        after = self.snapshot()
        cut = Path(rep["incomplete"][0]).name
        self.assertNotIn(cut, [Path(p).name for p in rep["written"]])
        self.assertNotEqual(after[cut], self.before[cut])

    def test_unwritable_target_is_refused(self):
        real = os.access

        def fake(path, mode, *a, **kw):
            if os.path.basename(str(path)) == "mid.v" and mode == os.W_OK:
                return False
            return real(path, mode, *a, **kw)
        code, out = self.run_main(mock.patch("os.access", fake))
        self.assertEqual(code, 1, out)
        self.assertIn("read-only", out)
        self.assertEqual(self.before, self.snapshot())
        self.assertEqual(self.temps(), [])
        self.assertIsNone(self.report())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores permission bits, so a read-only file is writable")
    def test_read_only_file_is_refused(self):
        mid = self.rtl / "mid.v"
        mid.chmod(0o444)
        try:
            code, out = self.run_main()
            self.assertEqual(code, 1, out)
            self.assertIn("read-only", out)
            self.assertEqual(self.before, self.snapshot())
            self.assertEqual(self.temps(), [])
        finally:
            mid.chmod(0o644)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_mode_is_kept(self):
        (self.rtl / "mid.v").chmod(0o640)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(stat.S_IMODE((self.rtl / "mid.v").stat().st_mode), 0o640)

    @unittest.skipUnless(os.name == "posix", "symlinks need no privilege on POSIX")
    def test_symlinked_file_stays_a_symlink(self):
        real_dir = Path(tempfile.mkdtemp())
        try:
            real = real_dir / "mid_real.v"
            shutil.move(str(self.rtl / "mid.v"), str(real))
            (self.rtl / "mid.v").symlink_to(real)
            code, out = self.run_main()
            self.assertEqual(code, 0, out)
            self.assertTrue((self.rtl / "mid.v").is_symlink())
            self.assertIn("AUTO_WIRE_END", real.read_text())
            self.assertEqual(sorted(p.name for p in real_dir.iterdir()), ["mid_real.v"])
        finally:
            shutil.rmtree(real_dir, ignore_errors=True)

    def test_directory_without_create_permission_falls_back_to_in_place(self):
        code, out = self.run_main(
            mock.patch("tempfile.mkstemp", side_effect=PermissionError(13, "Permission denied")))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.report()["status"], "written")
        expect = Path(tempfile.mkdtemp())
        try:
            write_fixture(expect)
            run_tool(expect, write_csv(expect, "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,route down"),
                     extra=("--yes",))
            self.assertEqual(self.snapshot(), {p.name: p.read_bytes()
                                               for p in sorted(expect.glob("*.v"))})
        finally:
            shutil.rmtree(expect, ignore_errors=True)

    def test_edit_after_staging_writes_nothing(self):
        top = self.rtl / "top.v"
        real = os.fsync
        calls = []

        def fsync_then_edit(fd):
            real(fd)
            calls.append(fd)
            if len(calls) == 3:                         # all three files are staged
                top.write_bytes(top.read_bytes() + b"// edited meanwhile\n")
        code, out = self.run_main(mock.patch("os.fsync", fsync_then_edit))
        self.assertEqual(code, 3, out)
        self.assertEqual(self.report()["status"], "stale")
        after = self.snapshot()
        self.assertEqual(after["top.v"], self.before["top.v"] + b"// edited meanwhile\n")
        self.assertEqual(after["mid.v"], self.before["mid.v"])
        self.assertEqual(after["leaf.v"], self.before["leaf.v"])
        self.assertEqual(self.temps(), [])


# ── optional LLM review ──────────────────────────────────────────────────────
class LlmParseTests(unittest.TestCase):
    def test_keeps_valid_findings_only(self):
        reply = ('Here you go:\n```json\n['
                 '{"row": 2, "severity": "HIGH", "reason": "clock to data"},'
                 '{"row": "3", "severity": "low", "reason": "odd name"},'
                 '{"row": 9, "severity": "high", "reason": "unknown row"},'
                 '{"row": 2, "severity": "fatal", "reason": "bad severity"},'
                 '{"row": 3, "severity": "low", "reason": "  "},'
                 '{"row": true, "severity": "low", "reason": "bool row"},'
                 '"not an object"]\n```')
        self.assertEqual(aw._llm_parse(reply, {2, 3}), [
            {"row": 2, "severity": "high", "reason": "clock to data"},
            {"row": 3, "severity": "low", "reason": "odd name"}])

    def test_reply_without_array_is_an_error(self):
        with self.assertRaises(ValueError):
            aw._llm_parse("Everything looks fine.", {2})


class _FakeLLM:
    """An OpenAI-compatible /chat/completions endpoint on localhost."""
    def __init__(self, content):
        import http.server, threading
        fake = self
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fake.requests.append((self.path, dict(self.headers), json.loads(body)))
                data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LlmReviewTests(_TmpRTL):
    KEY = "sk-test-not-a-real-key"

    def setUp(self):
        super().setUp()
        write_fixture(self.rtl)
        self.csv = write_csv(self.rtl, "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,route down")
        self.log = self.rtl / "autowire_warn.log"

    def run_llm(self, url, *extra):
        return run_tool(self.rtl, self.csv,
                        extra=("--llm-review", "--llm-url", url, "--llm-model", "m1", *extra),
                        env_extra={"AUTOWIRE_LLM_API_KEY": self.KEY})

    def test_findings_reported_and_logged(self):
        llm = _FakeLLM('[{"row": 2, "severity": "medium", "reason": "data into a sink"}]')
        try:
            r = self.run_llm(llm.url)
        finally:
            llm.close()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("[medium] row 2 w_data: data into a sink", out)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("[LLM REVIEW]", log)
        self.assertIn("data into a sink", log)
        self.assertIn("sink_in", read_all(self.rtl)["leaf.v"])     # still written
        path, headers, body = llm.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers.get("Authorization"), f"Bearer {self.KEY}")
        self.assertEqual(body["model"], "m1")
        rows = json.loads(body["messages"][1]["content"])
        self.assertEqual(rows[0]["src"]["signal"], "data_in")
        self.assertEqual(rows[0]["dst"]["direction"], "new port")
        self.assertNotIn(self.KEY, out + log)

    def test_unreachable_llm_does_not_stop_the_run(self):
        llm = _FakeLLM("[]")
        url = llm.url
        llm.close()                                   # nothing listens there now
        r = self.run_llm(url)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("LLM review incomplete", out)
        self.assertIn("sink_in", read_all(self.rtl)["leaf.v"])

    def test_key_never_printed_on_bad_key(self):
        llm = _FakeLLM("[]")
        try:
            for key in (self.KEY + "\n", "sk-in\nside"):
                r = run_tool(self.rtl, self.csv,
                             extra=("--llm-review", "--llm-url", llm.url, "--llm-model", "m1"),
                             env_extra={"AUTOWIRE_LLM_API_KEY": key})
                out = r.stdout + r.stderr + self.log.read_text(encoding="utf-8")
                self.assertEqual(r.returncode, 0, out)
                self.assertNotIn("sk-", out)
        finally:
            llm.close()

    def test_non_text_reply_does_not_stop_the_run(self):
        for content in (123, {"a": 1}, [{"row": 2}]):
            llm = _FakeLLM(content)
            try:
                r = self.run_llm(llm.url)
            finally:
                llm.close()
            out = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, out)
            self.assertIn("LLM review incomplete", out)

    def test_hostile_replies_do_not_stop_the_run(self):
        cases = {
            "deep": ("[" * 100000 + "]" * 100000, "LLM review incomplete"),
            "surrogate": ('[{"row": 2, "severity": "low", "reason": "bad \\ud800 x"}]',
                          "[low] row 2"),
            "echo": (f'[{{"row": 2, "severity": "low", "reason": "key {self.KEY}"}}]',
                     "key ***"),
        }
        for name, (content, needle) in cases.items():
            llm = _FakeLLM(content)
            try:
                r = self.run_llm(llm.url)
            finally:
                llm.close()
            out = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, f"{name}: {out[-2000:]}")
            log = self.log.read_text(encoding="utf-8")
            self.assertIn(needle, out + log, name)
            self.assertNotIn(self.KEY, out + log, name)

    def test_no_flag_no_request(self):
        llm = _FakeLLM("[]")
        try:
            r = run_tool(self.rtl, self.csv,
                         env_extra={"AUTOWIRE_LLM_URL": llm.url, "AUTOWIRE_LLM_MODEL": "m1"})
        finally:
            llm.close()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(llm.requests, [])
        self.assertNotIn("LLM REVIEW", r.stdout)

    def test_flag_without_url_is_an_error(self):
        r = run_tool(self.rtl, self.csv, extra=("--llm-review",),
                     env_extra={"AUTOWIRE_LLM_URL": "", "AUTOWIRE_LLM_MODEL": ""})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--llm-url", r.stderr)


# ── optional external-linter cross-check ─────────────────────────────────────
class LintTests(unittest.TestCase):
    LINTER = shutil.which("iverilog") or shutil.which("verilator")

    @unittest.skipUnless(LINTER, "no iverilog/verilator on PATH")
    def test_generated_rtl_lints_clean(self):
        tmp = tempfile.mkdtemp()
        try:
            rtl = Path(tmp)
            write_fixture(rtl)
            csv = rtl / "conn.csv"
            csv.write_text(
                "wire_name,bit_width,src,dst,comment\n"
                "w_data,8,top.data_in,top/u_mid/u_leaf.sink_in,route down\n",
                newline="\n")
            self.assertEqual(run_tool(rtl, csv).returncode, 0)
            vfiles = sorted(glob.glob(os.path.join(tmp, "*.v")))
            if "iverilog" in (self.LINTER or ""):
                cmd = ["iverilog", "-t", "null", "-Wall", "-s", "top", *vfiles]
            else:
                cmd = ["verilator", "--lint-only", "-Wall", "--top-module", "top", *vfiles]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
