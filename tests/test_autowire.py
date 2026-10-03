"""Tests for autowire_v3.

Run with:  python -m unittest discover -s tests   (from the repo root)

Covers unit-level helpers, an end-to-end wiring run on a hermetic synthetic
fixture (assertions on the generated RTL), idempotency (a second run is
byte-identical), pyslang elaboration of the result, the missing-source fatal
path, the pyslang import-failure messages, and an optional external linter
cross-check (skipped if none installed).
"""
import os
import sys
import glob
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

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


def run_tool(rtldir, csvpath, top="top", extra=()):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, AUTOWIRE, "-d", str(rtldir), "-T", top,
         "-c", str(csvpath), "--no-color", *extra],
        input="y\n", capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
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
    for f in sorted(glob.glob(os.path.join(str(rtldir), "*.v"))):
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
