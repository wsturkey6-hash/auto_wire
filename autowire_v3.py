#!/usr/bin/env python3
"""
autowire_v3.py  ─  SoC Integration Auto-Wiring Tool  v3.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Point-to-point CSV driven wiring  (no more --ip-path / --module)

  CSV FORMAT  (connections.csv)
  ─────────────────────────────────────────────────────────────────
  # Lines starting with # are comments and are ignored
  # Blank lines are ignored
  #
  # Columns:
  #   wire_name  : name of the crossing wire (leave blank = auto)
  #   src        : source endpoint  → HIER/PATH.port_name
  #   dst        : dest  endpoint  → HIER/PATH.port_name
  #   comment    : optional free text (ignored by tool)
  #
  wire_name,src,dst,comment
  w_sensor_data,TOP/u_subsys_b.sb_sensor_data,TOP/u_subsys_a/u_new_ip.sensor_data_i,Sensor bus
  ,TOP/u_subsys_c.portb,TOP/u_subsys_d/u_subsys_dd/u_subsys_ddd.portb,auto name

  HIER/PATH is the hierarchy path to the *parent module* that contains
  the signal.  The final segment of the path is the instance name.
  If the signal lives at the top-level module use just the module name:
    TOP.my_clk

  USAGE
  ─────────────────────────────────────────────────────────────────
  python3 autowire_v3.py \\
      --rtl-dir ./rtl   \\   # scan ALL .v recursively
      --top     TOP     \\   # top-level module name
      --csv     connections.csv

  python3 autowire_v3.py --rtl-dir ./rtl --top TOP --csv conn.csv --dry-run
  python3 autowire_v3.py --rtl-dir ./rtl --top TOP --csv conn.csv --no-color
"""

import re, os, sys, csv, json, argparse, getpass
from datetime import date as dt_date
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from difflib import SequenceMatcher

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# ──────────────────────────────────────────────────────────────────────────────
# ANSI Colors
# ──────────────────────────────────────────────────────────────────────────────
USE_COLOR = True

class C:
    RESET='\033[0m'; BOLD='\033[1m'; DIM='\033[2m'
    RED='\033[91m'; GREEN='\033[92m'; YELLOW='\033[93m'
    BLUE='\033[94m'; MAGENTA='\033[95m'; CYAN='\033[96m'; WHITE='\033[97m'

def col(text, *codes):
    return (''.join(codes)+str(text)+C.RESET) if USE_COLOR else str(text)

def hr(w=72, ch='─'):
    return col(ch*w, C.DIM)

# ──────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class SigInfo:
    name:      str
    direction: str   # input | output | inout | wire | reg
    width:     int   # bit count
    width_str: str   # '[7:0]' or ''

@dataclass
class InstInfo:
    inst_name:   str
    module_name: str
    connections: Dict[str,str]

@dataclass
class ModuleDef:
    name:      str
    filepath:  str
    ports:     Dict[str,SigInfo] = field(default_factory=dict)
    wires:     Dict[str,SigInfo] = field(default_factory=dict)
    instances: Dict[str,InstInfo]= field(default_factory=dict)
    source:    str = ''

@dataclass
class ConnSpec:
    """One row from the connections CSV."""
    wire_name: str          # user-specified or auto-generated
    src_path:  str          # hierarchy path of source module, e.g. TOP/u_subsys_b
    src_port:  str          # signal/port name in that module
    dst_path:  str          # hierarchy path of dest module, e.g. TOP/u_subsys_a/u_new_ip
    dst_port:  str          # port name in that module
    comment:   str = ''
    bit_width: Optional[int] = None
    row_num:   int = 0

@dataclass
class MismatchInfo:
    wire_name:  str
    src_port:   str; src_w: int
    dst_port:   str; dst_w: int
    assign_lhs: str; assign_rhs: str
    note:       str

# Change tracking – one entry per module
@dataclass
class ModChanges:
    module_name: str
    port_adds:   List[Tuple[str,str,str]] = field(default_factory=list)
    # (port_name, direction, width_str)
    wire_adds:   List[Tuple[str,str]]     = field(default_factory=list)
    # (wire_name, width_str)
    assign_adds: List[Tuple[str,str,str]] = field(default_factory=list)
    # (lhs, rhs, comment)
    inst_updates:Dict[str,Dict[str,str]]  = field(default_factory=dict)
    # {inst_name: {port: signal}}

# ──────────────────────────────────────────────────────────────────────────────
# VERILOG PARSER
# ──────────────────────────────────────────────────────────────────────────────
_SKIP_KW = {
    'module','endmodule','input','output','inout','wire','reg','logic',
    'assign','always','initial','begin','end','if','else','case','endcase',
    'for','while','function','task','parameter','localparam','generate',
    'genvar','integer','real','time','supply0','supply1','posedge','negedge',
    'default','signed','unsigned','automatic'
}

def _strip_comments(text: str) -> str:
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.DOTALL)
    return text

def _parse_width(b: str) -> Tuple[int, str]:
    if not b: return 1, ''
    inner = b.strip().strip('[]')
    m = re.match(r'(\d+)\s*:\s*(\d+)', inner)
    if m:
        hi, lo = int(m.group(1)), int(m.group(2))
        return abs(hi - lo) + 1, f'[{inner}]'
    return 1, f'[{inner}]'

def _find_balanced(text: str, pos: int, oc='(', cc=')') -> int:
    depth = 1; i = pos + 1
    while i < len(text) and depth > 0:
        if text[i] == oc: depth += 1
        elif text[i] == cc: depth -= 1
        i += 1
    return i - 1

def parse_verilog_file(filepath: str) -> Optional[ModuleDef]:
    try: source = Path(filepath).read_text(errors='replace')
    except Exception: return None
    clean = _strip_comments(source)

    m = re.search(r'\bmodule\s+(\w+)', clean)
    if not m: return None
    mod_name = m.group(1)

    # Signals
    ports, wires = {}, {}
    # Also parse typed port declarations that may appear directly in the
    # module header (Verilog-2001 style), e.g. `input wire [7:0] foo` inside
    # the parentheses. These do not end with a semicolon and thus are not
    # captured by the general semicolon-based signal regex below.
    try:
        po = clean.find('(', m.end())
        if po >= 0:
            pc = _find_balanced(clean, po)
            header_text = clean[po+1:pc]
            header_sig_re = re.compile(
                r'\b(input|output|inout)\b'
                r'(?:\s+(?:wire|reg|logic|signed|unsigned))*'
                r'(\s*\[[^\]]*\])?'
                r'\s+([\w\s,]+)', re.MULTILINE)
            for hm in header_sig_re.finditer(header_text):
                hkw = hm.group(1)
                hw, hws = _parse_width(hm.group(2))
                for raw in re.split(r'[,\s]+', hm.group(3)):
                    n = raw.strip()
                    if re.fullmatch(r'\w+', n) and n not in _SKIP_KW:
                        si = SigInfo(n, hkw, hw, hws)
                        if hkw in ('input','output','inout'):
                            ports.setdefault(n, si)
                        else:
                            wires.setdefault(n, si)
    except Exception:
        # Best effort: don't fail parsing the file just because header parsing
        # encountered an unexpected structure.
        pass
    sig_re = re.compile(
        r'\b(input|output|inout|wire|reg)\b'
        r'(?:\s+(?:wire|reg|logic|signed|unsigned))*'
        r'(\s*\[[^\]]*\])?'
        r'\s+([\w\s,]+?)(?=;)', re.MULTILINE)
    for sm in sig_re.finditer(clean):
        kw = sm.group(1)
        w, ws = _parse_width(sm.group(2))
        for raw in re.split(r'[,\s]+', sm.group(3)):
            n = raw.strip()
            if re.fullmatch(r'\w+', n) and n not in _SKIP_KW:
                si = SigInfo(n, kw, w, ws)
                if kw in ('input','output','inout'):
                    ports.setdefault(n, si)
                else:
                    wires.setdefault(n, si)

    # Instances
    instances = {}
    inst_re = re.compile(
        r'\b([A-Za-z_]\w*)(?:\s*#\s*\([^)]*\))?\s+([A-Za-z_]\w*)\s*(\()',
        re.MULTILINE)
    for im in inst_re.finditer(clean):
        mn2, iname, _ = im.group(1), im.group(2), im.group(3)
        if mn2 in _SKIP_KW or iname in _SKIP_KW: continue
        cp = _find_balanced(clean, im.start(3))
        tail = clean[cp+1:cp+5].strip()
        if not tail.startswith(';'): continue
        conns = {}
        for pm in re.finditer(r'\.(\w+)\s*\(\s*([^)]*?)\s*\)', clean[im.start(3)+1:cp]):
            conns[pm.group(1)] = pm.group(2).strip()
        instances[iname] = InstInfo(iname, mn2, conns)

    return ModuleDef(mod_name, filepath, ports, wires, instances, source)

# ──────────────────────────────────────────────────────────────────────────────
# RTL DATABASE + HIERARCHY TREE
# ──────────────────────────────────────────────────────────────────────────────
class HierNode:
    def __init__(self, module_name, inst_name, path, parent=None):
        self.module_name = module_name
        self.inst_name   = inst_name
        self.path        = path
        self.parent      = parent
        self.children: List['HierNode'] = []

class RTLDatabase:
    def __init__(self):
        self.modules: Dict[str,ModuleDef] = {}
        self.root: Optional[HierNode] = None
        self.errors: List[str] = []

    # ── Scanning ──────────────────────────────────────────────────────────────
    def scan_dir(self, rtl_dir: str):
        count = 0
        for vf in sorted(Path(rtl_dir).rglob('*.v')):
            mod = parse_verilog_file(str(vf))
            if not mod: continue
            if mod.name in self.modules:
                self.errors.append(
                    f'DUPLICATE MODULE  "{mod.name}"\n'
                    f'  file1: {self.modules[mod.name].filepath}\n'
                    f'  file2: {vf}')
            else:
                self.modules[mod.name] = mod; count += 1
        return count

    def check_duplicates(self) -> List[str]:
        errs = []
        for mod in self.modules.values():
            # Detect names that appear both as a port and as a wire/reg.
            # Common Verilog style is to declare a port and then a reg
            # with the same name (e.g. `output cmd_ack;\nreg cmd_ack;`).
            # Treat the specific case of port.direction=='output' and
            # wire.direction=='reg' as acceptable (not a duplicate).
            port_names = set(mod.ports.keys())
            wire_names = set(mod.wires.keys())
            for n in port_names & wire_names:
                p = mod.ports.get(n)
                w = mod.wires.get(n)
                # Allow common, non-problematic coexistence patterns:
                #  - output port implemented as reg (e.g. "output foo; reg foo;")
                #  - any port with a separate "wire" declaration (redundant but seen in RTL)
                allowed = False
                if p and w:
                    if w.direction == 'wire':
                        allowed = True
                    elif p.direction == 'output' and w.direction == 'reg':
                        allowed = True

                if not allowed:
                    errs.append(
                        f'[{mod.name}  {mod.filepath}]  '
                        f'duplicate signal "{n}"')
        return errs

    # ── Hierarchy ─────────────────────────────────────────────────────────────
    def build_hierarchy(self, top: str):
        if top not in self.modules:
            self.errors.append(f'Top module "{top}" not found in scanned files')
            return
        self.root = HierNode(top, top, top)
        self._recurse(self.root, set())

    def _recurse(self, node: HierNode, visited: set):
        mod = self.modules.get(node.module_name)
        if not mod or node.module_name in visited: return
        vis2 = visited | {node.module_name}
        for iname, inst in mod.instances.items():
            cp = f'{node.path}/{iname}'
            child = HierNode(inst.module_name, iname, cp, parent=node)
            node.children.append(child)
            self._recurse(child, vis2)

    def node(self, path: str) -> Optional[HierNode]:
        if not self.root: return None
        parts = path.split('/')
        n = self.root
        if n.inst_name != parts[0] and n.module_name != parts[0]:
            return None
        for p in parts[1:]:
            n = next((c for c in n.children if c.inst_name == p), None)
            if not n: return None
        return n

    def module_at(self, path: str) -> Optional[ModuleDef]:
        n = self.node(path)
        return self.modules.get(n.module_name) if n else None

    def mod_name_at(self, path: str) -> str:
        n = self.node(path)
        return n.module_name if n else path.split('/')[-1].upper()

    def sig_info(self, path: str, sig_name: str) -> Optional[SigInfo]:
        mod = self.module_at(path)
        if not mod: return None
        return mod.ports.get(sig_name) or mod.wires.get(sig_name)

    def print_tree(self, node: HierNode = None, depth: int = 0):
        if node is None: node = self.root
        if not node: return
        prefix = '  ' * depth
        inst_part = f'/{node.inst_name}' if node.inst_name != node.module_name else ''
        print(f'{prefix}{col(node.module_name, C.CYAN)}{col(inst_part, C.DIM)}')
        for ch in node.children:
            self.print_tree(ch, depth+1)

# ──────────────────────────────────────────────────────────────────────────────
# CSV PARSER
# ──────────────────────────────────────────────────────────────────────────────
def _parse_endpoint(raw: str, row_num: int) -> Tuple[str, str]:
    """
    Parse 'TOP/u_subsys_b.sb_sensor_data' → ('TOP/u_subsys_b', 'sb_sensor_data')
    Also accepts 'TOP/u_subsys_b/sb_sensor_data' (slash instead of dot).
    """
    raw = raw.strip()
    # prefer dot separator
    if '.' in raw:
        idx = raw.rfind('.')
        return raw[:idx], raw[idx+1:]
    # fallback: last slash segment is the port name
    if '/' in raw:
        idx = raw.rfind('/')
        return raw[:idx], raw[idx+1:]
    raise ValueError(
        f'Row {row_num}: cannot parse endpoint "{raw}". '
        f'Expected format: HIER/PATH.port_name')

def parse_connections_csv(filepath: str) -> List[ConnSpec]:
    conns: List[ConnSpec] = []
    with open(filepath, newline='', errors='replace') as f:
        lines = f.readlines()

    reader_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        reader_lines.append(stripped)

    if not reader_lines:
        return []

    # Auto-detect delimiter
    sample = '\n'.join(reader_lines[:5])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',\t|')
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(reader_lines, dialect)
    rows = list(reader)
    if not rows:
        return []

    # Detect header row (has no dots/slashes in first non-comment cell)
    start = 0
    h = [c.lower().strip() for c in rows[0]]
    if any(k in h for k in ('wire_name','src','dst','source','destination')):
        start = 1  # skip header

    for ridx, row in enumerate(rows[start:], start=start+1):
        # Pad to 5 columns to optionally accept a bit-width column
        while len(row) < 5: row.append('')
        wire_name = row[0].strip()
        # If the second column is a number, treat it as bit_width (new format):
        bit_width = None
        if row[1].strip() and re.fullmatch(r"\d+", row[1].strip()):
            bit_width = int(row[1].strip())
            src_raw = row[2].strip()
            dst_raw = row[3].strip()
            comment = row[4].strip() if len(row) > 4 else ''
        else:
            src_raw   = row[1].strip()
            dst_raw   = row[2].strip()
            comment   = row[3].strip() if len(row) > 3 else ''

        if not src_raw or not dst_raw:
            continue   # skip empty rows

        try:
            src_path, src_port = _parse_endpoint(src_raw, ridx)
            dst_path, dst_port = _parse_endpoint(dst_raw, ridx)
        except ValueError as e:
            raise ValueError(str(e))

        # Auto-generate wire name if blank
        if not wire_name:
            wire_name = f'w_{src_port}_to_{dst_port}'

        # Validate identifier
        if not re.fullmatch(r'[A-Za-z_]\w*', wire_name):
            raise ValueError(f'Row {ridx}: invalid wire_name "{wire_name}"')

        conns.append(ConnSpec(wire_name, src_path, src_port,
                      dst_path, dst_port, comment, bit_width, ridx))
    return conns

def parse_connections_xlsx(filepath: str, sheet=0) -> List[ConnSpec]:
    if not HAS_OPENPYXL:
        sys.exit(col('ERROR: pip install openpyxl', C.RED))
    wb  = openpyxl.load_workbook(filepath, data_only=True)
    ws  = wb.worksheets[sheet] if isinstance(sheet, int) else wb[sheet]
    rows = [[str(c) if c is not None else '' for c in r]
            for r in ws.iter_rows(values_only=True)]
    if not rows: return []

    # Write rows to a temp CSV-like structure and reuse CSV logic
    import tempfile, io
    buf = io.StringIO()
    writer = csv.writer(buf)
    for r in rows: writer.writerow(r)
    buf.seek(0)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv',
                                     delete=False, encoding='utf-8') as tf:
        tf.write(buf.getvalue())
        tname = tf.name
    result = parse_connections_csv(tname)
    os.unlink(tname)
    return result

# ──────────────────────────────────────────────────────────────────────────────
# LCA UTILITY
# ──────────────────────────────────────────────────────────────────────────────
def _lca(pa: str, pb: str) -> str:
    a, b = pa.split('/'), pb.split('/')
    common = []
    for x, y in zip(a, b):
        if x == y: common.append(x)
        else: break
    return '/'.join(common) if common else pa.split('/')[0]

def _levels_between(child: str, ancestor: str) -> List[str]:
    """Return instance names from just-below-ancestor down to child (inclusive)."""
    if child == ancestor: return []
    tail = child[len(ancestor):].lstrip('/')
    return tail.split('/')

# ──────────────────────────────────────────────────────────────────────────────
# CHANGE ACCUMULATOR
# ──────────────────────────────────────────────────────────────────────────────
class ChangeSet:
    """Accumulates all module changes across all connection specs."""

    def __init__(self):
        self._changes: Dict[str, ModChanges] = {}

    def _get(self, mod_name: str) -> ModChanges:
        return self._changes.setdefault(mod_name, ModChanges(mod_name))

    def add_port(self, mod_name: str, port_name: str,
                 direction: str, width_str: str):
        mc = self._get(mod_name)
        key = (port_name, direction, width_str)
        if key not in mc.port_adds:
            mc.port_adds.append(key)

    def add_wire(self, mod_name: str, wire_name: str, width_str: str):
        mc = self._get(mod_name)
        key = (wire_name, width_str)
        if key not in mc.wire_adds:
            mc.wire_adds.append(key)

    def add_assign(self, mod_name: str, lhs: str, rhs: str, comment: str = ''):
        mc = self._get(mod_name)
        key = (lhs, rhs, comment)
        if key not in mc.assign_adds:
            mc.assign_adds.append(key)

    def update_inst(self, mod_name: str, inst_name: str,
                    port_name: str, signal_name: str):
        mc = self._get(mod_name)
        mc.inst_updates.setdefault(inst_name, {})[port_name] = signal_name

    def all_modules(self) -> Dict[str, ModChanges]:
        return self._changes

# ──────────────────────────────────────────────────────────────────────────────
# CONNECTION PLANNER
# ──────────────────────────────────────────────────────────────────────────────
def plan_connection(db: RTLDatabase, spec: ConnSpec,
                    cs: ChangeSet, warnings: List[str],
                    mismatches: List[MismatchInfo]):
    """
    Plan all RTL changes required to route spec.src_path.spec.src_port
    to spec.dst_path.spec.dst_port via spec.wire_name.

    Strategy:
      1. Determine LCA of src_path and dst_path
      2. Bubble source signal UP to LCA (add output ports + wire at each level)
      3. Declare crossing wire at LCA
      4. Bubble wire DOWN to dst_path (add input ports at each level)
      5. Connect wire to dst_port at dst_path's parent
    """
    src_path  = spec.src_path
    src_port  = spec.src_port
    dst_path  = spec.dst_path
    dst_port  = spec.dst_port
    wname     = spec.wire_name

    # ── Get width info ────────────────────────────────────────────────────────
    src_si = db.sig_info(src_path, src_port)
    # dst info: look up the module instantiated at dst_path, find the port there
    dst_node = db.node(dst_path)
    dst_si: Optional[SigInfo] = None
    if dst_node:
        dst_mod = db.modules.get(dst_node.module_name)
        if dst_mod:
            dst_si = dst_mod.ports.get(dst_port) or dst_mod.wires.get(dst_port)

    # If CSV specifies a desired bit width, use it for the crossing wire.
    use_w  = spec.bit_width if getattr(spec, 'bit_width', None) else (src_si.width if src_si else 1)
    src_w  = src_si.width if src_si else 1
    # If destination port not found, assume it should match the crossing width
    # (CSV-provided or inferred). This avoids creating 1-bit adapters when the
    # destination module simply didn't parse earlier or is annotated elsewhere.
    dst_w  = dst_si.width if dst_si else use_w
    ws     = f'[{use_w-1}:0]' if use_w > 1 else ''

    # ── Width mismatch (handle dst-side adaptation). We adapt crossing wire
    # to the destination port width if necessary by inserting an adapter
    # wire+assign in the parent of the dst_path.
    adapted_name = wname   # what we connect at the dst end
    if dst_w != use_w:
        if dst_w > use_w:
            pad  = dst_w - use_w
            rhs  = f"{{{pad}'b0, {wname}}}"
            note = f'ZERO-PAD  {use_w}b -> {dst_w}b  ({pad} bits zero-padded on MSB)'
        else:
            rhs  = f'{wname}[{dst_w-1}:0]'
            note = f'TRUNCATE  {use_w}b -> {dst_w}b  (upper {use_w-dst_w} bits discarded)'

        adapted_name = f'w_adapt_{wname}'
        adapt_ws     = f'[{dst_w-1}:0]' if dst_w > 1 else ''

        # The adapt wire + assign live at the LCA-to-dst chain start,
        # concretely inside the parent of dst_path
        dst_parent  = dst_path.rsplit('/', 1)[0] if '/' in dst_path else dst_path
        dst_par_mod = db.mod_name_at(dst_parent)
        cs.add_wire  (dst_par_mod, adapted_name, adapt_ws)
        cs.add_assign(dst_par_mod, adapted_name, rhs,
                  f'WIDTH MISMATCH {use_w}b->{dst_w}b')

        mis = MismatchInfo(
            wire_name=wname,
            src_port=src_port, src_w=src_w,
            dst_port=dst_port, dst_w=dst_w,
            assign_lhs=adapted_name, assign_rhs=rhs, note=note)
        mismatches.append(mis)
        warnings.append(
            f'  {col("⚠ MISMATCH", C.YELLOW, C.BOLD)}'
            f'  [{spec.row_num}] {wname}: {src_port}({src_w}b) -> '
            f'{dst_port}({dst_w}b)  ->  {note}')

    # ── LCA computation ───────────────────────────────────────────────────────
    lca = _lca(src_path, dst_path)
    lca_mod = db.mod_name_at(lca)

    same_scope = (src_path == dst_path)

    # ── Same-scope shortcut (both endpoints in the same module) ───────────────
    if same_scope:
        # Just declare wire + connect both ends
        cs.add_wire(lca_mod, wname, ws)
        # src side: connect src_port to wire via assign
        cs.add_assign(lca_mod, wname, src_port, f'same-scope alias for {src_port}')
        # dst side: update instantiation if dst is an inst inside this module
        # dst_path == src_path, dst_port is a port on an instance here
        # We need to find which instance holds dst_port
        _connect_to_dst(db, spec, dst_path, dst_port, adapted_name, cs)
        return

    # ── Declare crossing wire at LCA ──────────────────────────────────────────
    cs.add_wire(lca_mod, wname, ws)

    # ── Bubble UP: src_path → LCA ─────────────────────────────────────────────
    _bubble_up(db, src_path, src_port, lca, wname, ws, cs, use_w)

    # ── Bubble DOWN: LCA → dst_path ───────────────────────────────────────────
    _bubble_down(db, lca, dst_path, wname, ws, adapted_name, dst_port, cs)


def _bubble_up(db: RTLDatabase,
               src_path: str, src_port: str,
               lca: str, wire_name: str, ws: str,
               cs: ChangeSet, use_w: int):
    """
    Add output ports at each level from src_path up to (but not including) lca,
    and update instantiation connections at each parent.
    """
    if src_path == lca:
        # Source signal lives in the LCA module's own scope (e.g. a top-level
        # port routed down into a sub-block). Drive the crossing wire directly
        # from it, padding/truncating if the requested width differs.
        lca_mod = db.mod_name_at(lca)
        src_si  = db.sig_info(src_path, src_port)
        if src_si and src_si.width != use_w:
            rhs = (f"{{{use_w-src_si.width}'b0, {src_port}}}" if use_w > src_si.width
                   else f"{src_port}[{use_w-1}:0]")
        else:
            rhs = src_port
        cs.add_assign(lca_mod, wire_name, rhs,
                      f'drive crossing wire from {src_port} at LCA scope')
        return

    levels = _levels_between(src_path, lca)  # instance names from lca down to src
    # e.g. src=TOP/u_sb/u_sensor, lca=TOP  → levels=['u_sb','u_sensor']

    # Bottom level: src_path module must expose src_port as output
    src_mod_name = db.mod_name_at(src_path)
    src_si = db.sig_info(src_path, src_port)
    # Decide whether to reuse existing output port or add a new output port
    # with the requested width `use_w`. If the existing output has a
    # different width, we add a new port and an assign that pads/truncates
    # the original signal to match `use_w`.
    if src_si and src_si.direction in ('output', 'inout') and src_si.width == use_w:
        # Already an output with matching width — expose directly
        expose_name = src_port
    else:
        # Add a new output port (named after the crossing wire)
        expose_name = wire_name
        src_mod_def = db.modules.get(src_mod_name)
        if not (src_mod_def and (expose_name in src_mod_def.ports or expose_name in src_mod_def.wires)):
            cs.add_port(src_mod_name, expose_name, 'output', ws)

        # If we know the source signal width, add an assign to adapt widths
        if src_si:
            if src_si.width != use_w:
                # generate pad or truncate rhs
                if use_w > src_si.width:
                    pad = use_w - src_si.width
                    rhs = f"{{{pad}'b0, {src_port}}}"
                    note = f'ZERO-PAD {src_si.width}b->{use_w}b'
                else:
                    rhs = f'{src_port}[{use_w-1}:0]'
                    note = f'TRUNCATE {src_si.width}b->{use_w}b'
                cs.add_assign(src_mod_name, expose_name, rhs,
                              f'WIDTH MISMATCH {src_si.width}b->{use_w}b')
            else:
                # same width but not an output previously — expose directly
                if src_si.direction != 'output':
                    cs.add_assign(src_mod_name, expose_name, src_port,
                                  f'expose internal {src_port} for hierarchy crossing')

    # Walk UP from src toward lca
    # levels[0] = instance just below lca, levels[-1] = instance at src_path
    current_sig = expose_name

    for depth in range(len(levels) - 1, -1, -1):
        child_path   = lca + '/' + '/'.join(levels[:depth+1])
        parent_path  = lca + '/' + '/'.join(levels[:depth]) if depth > 0 else lca
        child_mod    = db.mod_name_at(child_path)
        parent_mod   = db.mod_name_at(parent_path)
        inst_name    = levels[depth]

        if depth < len(levels) - 1:
            # Intermediate module: add both input (from below) + output (to above)
            # Only add the output port in the child if it doesn't already exist.
            child_def = db.modules.get(child_mod)
            if not (child_def and (wire_name in child_def.ports or wire_name in child_def.wires)):
                cs.add_port(child_mod, wire_name, 'output', ws)
            # Connect from what came in from below to this new output (or existing)
            if depth + 1 <= len(levels) - 1:
                cs.add_assign(child_mod, wire_name, current_sig,
                              'pass-through hierarchy crossing')

        # In parent: update instantiation of inst_name to connect wire_name
        sig_in_parent = wire_name if depth > 0 else wire_name
        cs.update_inst(parent_mod, inst_name, current_sig, sig_in_parent)
        current_sig = sig_in_parent


def _bubble_down(db: RTLDatabase,
                 lca: str, dst_path: str,
                 wire_name: str, ws: str,
                 adapted_name: str,  # wire to actually connect at dst
                 dst_port: str,
                 cs: ChangeSet):
    """
    Thread wire_name DOWN from lca to the parent of dst_path,
    then connect adapted_name to dst_port.
    """
    if dst_path == lca:
        # dst lives in the LCA module's own scope (e.g. a sub-block output
        # routed up to a top-level output port). Connect the crossing wire to
        # dst_port there, adding the output port if the module lacks it.
        lca_mod = db.mod_name_at(lca)
        lca_def = db.modules.get(lca_mod)
        if not (lca_def and dst_port in lca_def.ports):
            cs.add_port(lca_mod, dst_port, 'output', ws)
        cs.add_assign(lca_mod, dst_port, adapted_name,
                      f'drive LCA-scope {dst_port} from crossing wire')
        return

    levels = _levels_between(dst_path, lca)  # [inst_below_lca, ..., dst_inst]

    for depth, inst_name in enumerate(levels):
        child_path  = lca + '/' + '/'.join(levels[:depth+1])
        parent_path = lca + '/' + '/'.join(levels[:depth]) if depth > 0 else lca
        child_mod   = db.mod_name_at(child_path)
        parent_mod  = db.mod_name_at(parent_path)

        is_last = (depth == len(levels) - 1)

        if is_last:
            # dst level: ensure the child module actually exposes dst_port.
            # If the instantiated module lacks the port, add it there.
            child_def = db.modules.get(child_mod)
            if child_def and dst_port in child_def.ports:
                # Child already exposes the port — reuse it
                pass
            else:
                # Child missing the port -> add it as an input on the child module
                # (only if not already scheduled)
                if not (child_def and dst_port in (child_def.ports or {})):
                    cs.add_port(child_mod, dst_port, 'input', ws)

            # Update the instantiation in the parent to connect the child's port
            cs.update_inst(parent_mod, inst_name, dst_port, adapted_name)

            # The parent module needs input port wire_name if it's not the LCA
            if parent_path != lca:
                parent_def = db.modules.get(parent_mod)
                if not (parent_def and wire_name in parent_def.ports):
                    cs.add_port(parent_mod, wire_name, 'input', ws)
        else:
            # Intermediate level: add input port for wire_name (if needed)
            child_def = db.modules.get(child_mod)
            if not (child_def and (wire_name in child_def.ports or wire_name in child_def.wires)):
                cs.add_port(child_mod, wire_name, 'input', ws)
            # Parent updates instantiation of this inst
            cs.update_inst(parent_mod, inst_name, wire_name, wire_name)

    # The direct parent of dst needs the wire available
    if len(levels) > 1:
        dst_parent_path = dst_path.rsplit('/', 1)[0]
        dst_par_mod     = db.mod_name_at(dst_parent_path)
        if dst_parent_path != lca:
            par_def = db.modules.get(dst_par_mod)
            if not (par_def and wire_name in par_def.ports):
                cs.add_port(dst_par_mod, wire_name, 'input', ws)


def _connect_to_dst(db, spec, scope_path, dst_port, signal_name, cs):
    """
    In same-scope case: find which instance holds dst_port and update it.
    """
    scope_mod = db.module_at(scope_path)
    if not scope_mod: return
    for iname, inst in scope_mod.instances.items():
        if dst_port in inst.connections or (
            db.modules.get(inst.module_name) and
                dst_port in (db.modules[inst.module_name].ports or {})
        ):
            cs.update_inst(db.mod_name_at(scope_path), iname, dst_port, signal_name)
            return

# ──────────────────────────────────────────────────────────────────────────────
# WRITE-BACK ENGINE
# ──────────────────────────────────────────────────────────────────────────────
MARK_BEGIN = '// AUTO_WIRE_BEGIN'
MARK_END   = '// AUTO_WIRE_END'

def _stamp(user: str) -> str:
    return f'user:{user}  date:{dt_date.today()}'

# SHORT stamp for inline use – must NOT contain MARK_BEGIN/END text
_INLINE_STAMP_PREFIX = '// aw:'

def _inline_stamp(user: str) -> str:
    return f'{_INLINE_STAMP_PREFIX}{user} {dt_date.today()}'

def _mask_comments(text: str) -> str:
    """
    Return a copy of text with comment CONTENT replaced by spaces.
    Length is preserved → positions in masked string == positions in original.
    """
    result = list(text)
    i = 0
    while i < len(text):
        if text[i:i+2] == '//':
            j = text.find('\n', i)
            if j < 0: j = len(text)
            for k in range(i, j): result[k] = ' '
            i = j
        elif text[i:i+2] == '/*':
            j = text.find('*/', i + 2)
            end = (j + 2) if j >= 0 else len(text)
            for k in range(i, end): result[k] = ' '
            i = end
        else:
            i += 1
    return ''.join(result)

def _find_balanced(text: str, pos: int, oc: str = '(', cc: str = ')') -> int:
    """Find position of the matching close-char starting from pos (open-char)."""
    depth = 1; i = pos + 1
    while i < len(text) and depth > 0:
        if   text[i] == oc: depth += 1
        elif text[i] == cc: depth -= 1
        i += 1
    return i - 1

def _build_auto_block(mc: ModChanges, user: str) -> str:
    lines = [f'{MARK_BEGIN}  {_stamp(user)}  tool:autowire_v3.py', '']

    if mc.port_adds:
        # List ports that were added to the module header (do not re-declare
        # them inside the AUTO_WIRE block to avoid duplicate declarations).
        lines.append('// --- Ports added to module header (listed for reference) ----')
        for pname, pdir, pws in mc.port_adds:
            ws_str = f' {pws}' if pws else ''
            lines.append(f'//   {pdir} wire{ws_str} {pname}')
        lines.append('')

    if mc.wire_adds:
        lines.append('// --- Crossing / adapter wire declarations ---------------------')
        for wname, wws in mc.wire_adds:
            ws_str = f' {wws}' if wws else ''
            lines.append(f'wire{ws_str} {wname};')
        lines.append('')

    if mc.assign_adds:
        lines.append('// --- Width-adapter / pass-through assigns ---------------------')
        for lhs, rhs, cmt in mc.assign_adds:
            c_str = f'  // {cmt}' if cmt else ''
            lines.append(f'assign {lhs} = {rhs};{c_str}')
        lines.append('')

    lines.append(MARK_END)
    return '\n'.join(lines)

def _insert_inst_connections(source: str, inst_name: str,
                              new_conns: Dict[str, str], user: str) -> str:
    """
    Insert .port(signal) entries into an existing instantiation.
    Works directly on source with comment-masked positions to avoid offset bugs.
    """
    masked = _mask_comments(source)          # same length as source
    pat = re.compile(
        r'\b\w+\s*(?:#\s*\([^)]*\))?\s*'
        + re.escape(inst_name) + r'\s*\(',
        re.MULTILINE)
    m = pat.search(masked)
    if not m: return source

    # open_pos = the '(' that starts the port connection list
    open_pos  = masked.rfind('(', 0, m.end())
    close_pos = _find_balanced(masked, open_pos)   # positions valid for source too

    # Replace any existing .port(...) entries for ports we are updating,
    # and append any missing connections (keeping the inline-stamp format
    # for later idempotent removal).
    conn_text = source[open_pos+1:close_pos]
    conn_re = re.compile(r'\.\s*(\w+)\s*\(\s*([^)]*?)\s*\)', re.MULTILINE)

    existing_ports = set(m.group(1) for m in conn_re.finditer(conn_text))

    def _repl(m):
        pname = m.group(1)
        if pname in new_conns:
            return f'.{pname} ( {new_conns[pname]} )'
        return m.group(0)

    new_conn_text = conn_re.sub(_repl, conn_text)

    missing = [p for p in new_conns.keys() if p not in existing_ports]
    if missing:
        stamp = _inline_stamp(user)
        parts = [f',  {stamp}\n    .{p:<40} ( {new_conns[p]} )' for p in missing]
        additions = ''.join(parts)
        new_conn_text = new_conn_text.rstrip() + additions

    return source[:open_pos+1] + new_conn_text + source[close_pos:]

def _add_ports_to_header(source: str, mod_name: str,
                         port_decls: List[Tuple[str, str, str]],
                         user: str) -> str:
    """Add port declarations to the module header port list."""
    masked = _mask_comments(source)
    pat    = re.compile(r'\bmodule\s+' + re.escape(mod_name) + r'\b')
    m      = pat.search(masked)
    if not m: return source
    po = masked.find('(', m.end())
    if po < 0: return source
    pc    = _find_balanced(masked, po)
    stamp = _inline_stamp(user)
    # If module header already contains direction keywords (ANSI style)
    # insert typed declarations into the header. Otherwise (legacy style)
    # insert plain names in the header and add body-style declarations
    # separately.
    header_text = masked[po+1:pc]
    if re.search(r'\b(input|output|inout)\b', header_text):
        additions = ''.join(
            f',  {stamp}\n    {pdir} wire{(" "+pws) if pws else ""} {pname}'
            for pname, pdir, pws in port_decls)
    else:
        additions = ''.join(
            f',  {stamp}\n    {pname}'
            for pname, pdir, pws in port_decls)
    return source[:pc] + additions + source[pc:]

def _add_port_decls_to_body(source: str, mod_name: str,
                            port_decls: List[Tuple[str, str, str]],
                            user: str) -> str:
    """Insert semicolon-terminated port declarations into module body.
    For legacy (non-ANSI) modules where the header contains only identifiers,
    add lines like:
      // aw:user 2026-04-21
      input wire [7:0] foo;
    inserted immediately after the module header termination.
    If the module header already contains direction keywords (ANSI style),
    do not add body declarations to avoid duplicate declarations.
    """
    masked = _mask_comments(source)
    pat = re.compile(r'\bmodule\s+' + re.escape(mod_name) + r'\b')
    m = pat.search(masked)
    if not m:
        return source
    po = masked.find('(', m.end())
    if po < 0:
        return source
    pc = _find_balanced(masked, po)
    header_text = masked[po+1:pc]
    # If header already contains direction keywords, skip body insertion.
    if re.search(r'\b(input|output|inout)\b', header_text):
        return source
    # Find semicolon that terminates the module header (first semicolon after pc)
    sc = source.find(';', pc)
    insert_pos = sc + 1 if sc >= 0 else pc + 1
    stamp = _inline_stamp(user)
    decl_lines = []
    for pname, pdir, pws in port_decls:
        pws_str = f' {pws}' if pws else ''
        decl_lines.append(f'{stamp}\n    {pdir} wire{pws_str} {pname};\n')
    additions = '\n' + ''.join(decl_lines)
    return source[:insert_pos] + additions + source[insert_pos:]

def _replace_auto_block(source: str, block: str) -> str:
    """Replace existing AUTO_WIRE_BEGIN/END region, or insert before endmodule."""
    # Search in masked text so embedded MARK_BEGIN in inline stamps don't confuse us
    masked = _mask_comments(source)
    bi = masked.find(MARK_BEGIN)
    ei = masked.find(MARK_END)
    if bi >= 0 and ei >= 0:
        return source[:bi] + block + source[ei + len(MARK_END):]
    em = source.rfind('endmodule')
    if em < 0:
        return source + '\n' + block + '\n'
    return source[:em] + block + '\n\n' + source[em:]

def _strip_all_aw_content(source: str) -> str:
    """
    Remove ALL content previously written by this tool in one pass:
      1. The AUTO_WIRE_BEGIN … AUTO_WIRE_END block (and surrounding blank lines)
      2. Every inline-stamped port/connection line (,  // aw:… \n    …)
    Returns the source as close to its original human-written form as possible.
    """
    # ── 1. Remove AUTO_WIRE_BEGIN … AUTO_WIRE_END block ──────────────────────
    # Strip the block plus any preceding blank lines
    source = re.sub(
        r'\n*' + re.escape(MARK_BEGIN) + r'.*?' + re.escape(MARK_END) + r'\n?',
        '', source, flags=re.DOTALL)

    # ── 2. Remove inline-stamped entries ─────────────────────────────────────
    # Two patterns inserted by the tool:
    #
    # (a) Port in module header:
    #     ,  // aw:user date\n    <direction> wire [W] name
    #
    # (b) Port connection in instantiation:
    #     ,  // aw:user date\n    .port_name ( signal )
    aw_prefix = re.escape(_INLINE_STAMP_PREFIX)

    source = re.sub(
        r',[ \t]*' + aw_prefix + r'[^\n]*\n[ \t]*'
        r'(?:(?:input|output|inout)\s+wire(?:\s*\[[^\]]*\])?\s+\w+'   # header port
        r'|'
        r'\.\w+[ \t]*\([^)]*\))',                                       # inst conn
        '', source)

    # Also remove body-style inline-stamped declarations ending with semicolon,
    # e.g. "// aw:user 2026-04-21\n    input wire [7:0] foo;"
    source = re.sub(
        aw_prefix + r'[^\n]*\n[ \t]*(?:input|output|inout)\s+wire(?:\s*\[[^\]]*\])?\s+\w+\s*;',
        '', source)

    return source


def apply_changes(mod: ModuleDef, mc: ModChanges, user: str) -> str:
    # 0. Strip ALL previously generated content so every run is idempotent
    source = _strip_all_aw_content(mod.source)

    # 1. Update instantiation connections
    for inst_name, port_map in mc.inst_updates.items():
        source = _insert_inst_connections(source, inst_name, port_map, user)

    # 2. Add hierarchy-crossing ports to module header
    if mc.port_adds:
        source = _add_ports_to_header(source, mod.name, mc.port_adds, user)
        # Also add semicolon-terminated body-style declarations for legacy modules
        source = _add_port_decls_to_body(source, mod.name, mc.port_adds, user)

    # 3. Insert AUTO_WIRE_BEGIN … AUTO_WIRE_END block before endmodule
    block  = _build_auto_block(mc, user)
    source = _replace_auto_block(source, block)

    return source

# ──────────────────────────────────────────────────────────────────────────────
# REPORTING
# ──────────────────────────────────────────────────────────────────────────────
def print_plan_summary(conns: List[ConnSpec], cs: ChangeSet,
                       mismatches: List[MismatchInfo],
                       db: RTLDatabase):
    print(f'\n  {hr()}')
    print(col(f'  Plan Summary  ({len(conns)} connections)', C.WHITE, C.BOLD))
    print(f'  {hr()}')
    for spec in conns:
        lca = _lca(spec.src_path, spec.dst_path)
        print(f'\n  {col(spec.wire_name, C.CYAN, C.BOLD)}')
        print(f'    src : {col(spec.src_path, C.DIM)}.{col(spec.src_port, C.WHITE)}')
        print(f'    dst : {col(spec.dst_path, C.DIM)}.{col(spec.dst_port, C.WHITE)}')
        print(f'    LCA : {col(lca, C.YELLOW)}')
        if spec.comment:
            print(f'    note: {col(spec.comment, C.DIM)}')

    all_mods = sorted(cs.all_modules().keys())
    if all_mods:
        print(f'\n  {col("Modules to be modified:", C.BOLD)}')
        for mn in all_mods:
            mc = cs.all_modules()[mn]
            parts = []
            if mc.port_adds:   parts.append(f'+{len(mc.port_adds)}port')
            if mc.wire_adds:   parts.append(f'+{len(mc.wire_adds)}wire')
            if mc.assign_adds: parts.append(f'+{len(mc.assign_adds)}assign')
            if mc.inst_updates:
                total_conns = sum(len(v) for v in mc.inst_updates.values())
                parts.append(f'+{total_conns}conn')
            mf = db.modules[mn].filepath if mn in db.modules else '?'
            print(f'    {col(mn, C.WHITE):<28} '
                  f'{col(", ".join(parts), C.DIM):<30} '
                  f'{col(mf, C.DIM)}')

    if mismatches:
        print(f'\n  {col("⚠  Width mismatches:", C.YELLOW, C.BOLD)}')
        for ms in mismatches:
            print(f'    {col(ms.wire_name, C.CYAN)}  {ms.note}')
            print(f'      -> {col(f"assign {ms.assign_lhs} = {ms.assign_rhs};", C.DIM)}')

def generate_warn_log(conns: List[ConnSpec], mismatches: List[MismatchInfo],
                      user: str) -> str:
    lines = [
        f'AutoWire v3  Warning Log',
        f'{_stamp(user)}',
        '=' * 72, ''
    ]

    if mismatches:
        lines += [
            '[WIDTH MISMATCH]',
            'These connections have differing port widths.',
            'PLEASE REVIEW to confirm padding/truncation is semantically correct.',
            ''
        ]
        for ms in mismatches:
            lines += [
                f'  Wire       : {ms.wire_name}',
                f'  Source     : {ms.src_port}  ({ms.src_w}b)',
                f'  Dest       : {ms.dst_port}  ({ms.dst_w}b)',
                f'  Action     : {ms.note}',
                f'  Generated  : assign {ms.assign_lhs} = {ms.assign_rhs};',
                ''
            ]

    lines += [
        '[CONNECTIONS PROCESSED]',
        ''
    ]
    for spec in conns:
        lca = _lca(spec.src_path, spec.dst_path)
        lines += [
            f'  {spec.wire_name}',
            f'    src : {spec.src_path}.{spec.src_port}',
            f'    dst : {spec.dst_path}.{spec.dst_port}',
            f'    LCA : {lca}',
            ''
        ]

    if not mismatches:
        lines += ['No width warnings.', '']

    return '\n'.join(lines)

# ──────────────────────────────────────────────────────────────────────────────
# BANNER
# ──────────────────────────────────────────────────────────────────────────────
def print_banner():
    print(col("""
  ╔═══════════════════════════════════════════════════════════════════╗
  ║  AutoWire  v3  ─  SoC Integration  Point-to-Point Wiring Tool   ║
  ║  CSV-driven │ Full Hierarchy │ Width Mismatch │ Write-back        ║
  ╚═══════════════════════════════════════════════════════════════════╝""",
    C.CYAN, C.BOLD))

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main():
    # The banner and status glyphs (✓ ⚠ box-drawing) are non-ASCII. On a
    # console whose codec can't encode them (e.g. Windows cp950) printing them
    # raises UnicodeEncodeError and crashes the run. Emit UTF-8 instead, and
    # replace anything that still can't be encoded rather than aborting.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description='AutoWire v3: CSV-driven SoC point-to-point wiring',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CSV FORMAT (connections.csv)
  # comment lines start with #
  wire_name,src,dst,comment
  w_sensor,TOP/u_sb.sb_data,TOP/u_sa/u_ip.data_i,Sensor bus
  ,TOP/u_sc.portb,TOP/u_sd/u_sdd/u_sddd.portb   (auto wire name)

ENDPOINT FORMAT
  HIER/PATH.port_name   e.g.  TOP/u_subsys_b/u_sensor.data_out
  The HIER/PATH is the hierarchy path to the instance whose scope
  contains the signal. The last segment is the instance name.
""")
    ap.add_argument('--rtl-dir', '-d', required=True,
                    help='Root directory of RTL project (scanned recursively)')
    ap.add_argument('--top',     '-T', required=True,
                    help='Top-level module name')
    ap.add_argument('--csv',     '-c', required=True,
                    help='Connections CSV (or XLSX) file')
    ap.add_argument('--sheet',   '-s', default=0,
                    help='Excel sheet name or 0-based index (XLSX only)')
    ap.add_argument('--out-log', '-l', default=None,
                    help='Warning log output file (default: autowire_warn.log)')
    ap.add_argument('--dry-run',       action='store_true',
                    help='Preview all changes; do NOT write any files')
    ap.add_argument('--tree',          action='store_true',
                    help='Print the parsed hierarchy tree and exit')
    ap.add_argument('--no-color',      action='store_true',
                    help='Disable ANSI color output')

    args = ap.parse_args()

    global USE_COLOR
    if args.no_color or not sys.stdout.isatty():
        USE_COLOR = False

    user     = getpass.getuser()
    warn_log = args.out_log or 'autowire_warn.log'

    print_banner()
    print(f'\n  User      : {col(user,    C.WHITE, C.BOLD)}')
    print(f'  Top       : {col(args.top, C.CYAN,  C.BOLD)}')
    print(f'  RTL dir   : {col(args.rtl_dir, C.DIM)}')
    print(f'  CSV       : {col(args.csv,     C.DIM)}')

    # ── Scan RTL ──────────────────────────────────────────────────────────────
    db = RTLDatabase()
    count = db.scan_dir(args.rtl_dir)
    print(col(f'\n  Modules   : {count} parsed', C.GREEN))

    if db.errors:
        print(col('\n  ERRORS:', C.RED, C.BOLD))
        for e in db.errors: print(col(f'  {e}', C.RED))
        sys.exit(1)

    dup_errs = db.check_duplicates()
    if dup_errs:
        print(col('\n  DUPLICATE SIGNAL ERRORS (fix RTL before proceeding):', C.RED, C.BOLD))
        for e in dup_errs: print(col(f'  {e}', C.RED))
        sys.exit(1)

    db.build_hierarchy(args.top)
    if db.errors:
        print(col('\n  HIERARCHY ERRORS:', C.RED, C.BOLD))
        for e in db.errors: print(col(f'  {e}', C.RED))
        sys.exit(1)

    if args.tree:
        print(col('\n  Hierarchy tree:', C.BOLD))
        db.print_tree()
        sys.exit(0)

    print(col('  Hierarchy : built', C.GREEN))

    # ── Parse connections CSV ─────────────────────────────────────────────────
    ext = Path(args.csv).suffix.lower()
    try:
        if ext in ('.xlsx', '.xls', '.xlsm'):
            sh = int(args.sheet) if str(args.sheet).isdigit() else args.sheet
            conns = parse_connections_xlsx(args.csv, sh)
        else:
            conns = parse_connections_csv(args.csv)
    except ValueError as e:
        print(col(f'\n  CSV ERROR: {e}', C.RED))
        sys.exit(1)

    if not conns:
        print(col('\n  No connections found in CSV. Nothing to do.', C.YELLOW))
        sys.exit(0)

    print(col(f'  Connections: {len(conns)} loaded', C.GREEN))

    # ── Validate endpoints ────────────────────────────────────────────────────
    validation_errors = []
    for spec in conns:
        for side, path, port in [('src', spec.src_path, spec.src_port),
                                  ('dst', spec.dst_path, spec.dst_port)]:
            n = db.node(path)
            if n is None:
                validation_errors.append(
                    f'  Row {spec.row_num}: {side} path "{path}" '
                    f'not found in hierarchy')
                continue
            mod = db.modules.get(n.module_name)
            if mod and port not in mod.ports and port not in mod.wires:
                if side == 'src':
                    # A source must exist — you cannot drive a wire from a
                    # signal that is not declared. Fatal.
                    validation_errors.append(
                        f'  Row {spec.row_num}: src port "{port}" not found in '
                        f'module "{n.module_name}". A source signal must already '
                        f'exist (add it to the RTL first).')
                else:
                    # Soft warning, not fatal: the tool creates missing dst
                    # ports, or it may be on an IP not in rtl-dir.
                    print(col(f'  WARN row {spec.row_num}: '
                               f'{side} port "{port}" not found in module '
                               f'"{n.module_name}" (may be external IP)', C.YELLOW))

    if validation_errors:
        print(col('\n  PATH VALIDATION ERRORS:', C.RED, C.BOLD))
        for e in validation_errors: print(col(e, C.RED))
        print(col('\n  Use --tree to inspect the parsed hierarchy.', C.DIM))
        sys.exit(1)

    # ── Wire-name duplicate check ─────────────────────────────────────────────
    seen_names: Dict[str, int] = {}
    for spec in conns:
        if spec.wire_name in seen_names:
            print(col(f'\n  ERROR: wire_name "{spec.wire_name}" used in '
                      f'row {seen_names[spec.wire_name]} AND row {spec.row_num}. '
                      f'Each crossing wire must be unique.', C.RED, C.BOLD))
            sys.exit(1)
        seen_names[spec.wire_name] = spec.row_num

    # ── Plan all connections ──────────────────────────────────────────────────
    cs         = ChangeSet()
    warn_msgs: List[str] = []
    mismatches: List[MismatchInfo] = []

    for spec in conns:
        plan_connection(db, spec, cs, warn_msgs, mismatches)

    # ── Print plan summary ────────────────────────────────────────────────────
    print_plan_summary(conns, cs, mismatches, db)

    if warn_msgs:
        print()
        for w in warn_msgs: print(w)

    # ── Confirm before writing ────────────────────────────────────────────────
    if not args.dry_run:
        print(f'\n  {hr()}')
        affected = sorted(cs.all_modules().keys())
        print(col(f'  Will modify {len(affected)} module(s):', C.BOLD))
        for mn in affected:
            fp = db.modules[mn].filepath if mn in db.modules else col('? not in DB', C.RED)
            print(f'    {col(mn, C.WHITE):<28} {col(fp, C.DIM)}')
        print()

        try:
            ans = input(col('  Proceed with write-back? [Y/n]: ', C.YELLOW)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = 'n'

        if ans == 'n':
            print(col('  Aborted. No files written.', C.YELLOW))
            sys.exit(0)

        # ── Apply changes ─────────────────────────────────────────────────────
        written = 0
        for mn, mc in cs.all_modules().items():
            mod = db.modules.get(mn)
            if not mod:
                print(col(f'  WARN: module "{mn}" not in DB, skipping', C.YELLOW))
                continue
            new_src = apply_changes(mod, mc, user)
            if new_src != mod.source:
                Path(mod.filepath).write_text(new_src)
                print(col(f'  ✓  {mod.filepath}', C.GREEN))
                written += 1
            else:
                print(col(f'  ─  {mod.filepath}  (unchanged)', C.DIM))

        print(col(f'\n  {written} file(s) written.', C.GREEN if written else C.DIM))
    else:
        print(col('\n  [DRY RUN]  No files written.', C.YELLOW))

    # ── Write warning log ─────────────────────────────────────────────────────
    warn_content = generate_warn_log(conns, mismatches, user)
    with open(warn_log, 'w') as f: f.write(warn_content)
    if mismatches:
        print(col(f'  ⚠  {warn_log}  (width mismatches — please review)', C.YELLOW))
    else:
        print(col(f'  ✓  {warn_log}', C.GREEN))
    print()


if __name__ == '__main__':
    main()
