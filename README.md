# AutoWire — CSV-driven SoC point-to-point wiring

AutoWire reads a folder of Verilog RTL and a CSV of point-to-point connections,
then automatically threads each connection through the module hierarchy —
adding the ports, wires, `assign`s, and instantiation connections needed at
every level — and writes the changes back into the `.v` files inside clearly
marked, idempotent `// AUTO_WIRE_BEGIN … // AUTO_WIRE_END` blocks.

## How it works
1. **Parse** all `.v` and `.sv` files, and the `.vh` / `.svh` headers they
   include, with [pyslang](https://github.com/MikePopoloski/slang)
   (a real SystemVerilog/Verilog frontend) — accurate ports, directions, and
   concrete (parameter-resolved) bit widths.
2. **Elaborate** the design from the given top module to build the instance
   hierarchy.
3. For each connection, compute the **lowest common ancestor (LCA)** of the
   source and destination, "bubble" the signal up to the LCA and back down to
   the destination, inserting ports/wires/assigns/connections at each level.
4. **Write back** the edits idempotently — re-running reproduces the same
   result, because the tool strips its own previous output first.
   Each run makes the scanned RTL match the CSV: output from earlier runs that
   the CSV no longer asks for is removed from every scanned file, so keep all
   of a design's connections in one CSV. A file whose new content differs only
   in the user/date stamps is left as is, so a re-run by someone else or on
   another day changes nothing.

## Install
```
pip install -r requirements.txt
```
Requires Python 3 and `pyslang` (tested with 11.x and 12.x). Optionally install Icarus Verilog (`iverilog`)
or Verilator to enable the test suite's external elaboration/lint cross-check,
and `openpyxl` if your connection list is an `.xlsx` file.

## Usage
```
python autowire_v3.py --rtl-dir ./rtl --top TOP --csv connections.csv
python autowire_v3.py -d ./rtl -T TOP -c conn.csv --dry-run   # preview only
python autowire_v3.py -d ./rtl -T TOP --tree                  # print hierarchy
```

## Connection CSV format
Comment lines start with `#`; blank lines are ignored. The file may be UTF-8
(with or without a BOM, e.g. Excel's "CSV UTF-8") or in the system encoding.
```
wire_name,src,dst,comment
w_sensor,TOP/u_sb.sb_data,TOP/u_sa/u_ip.data_i,Sensor bus
,TOP/u_sc.portb,TOP/u_sd/u_sdd.portb,(blank wire_name = auto)
```
An optional integer `bit_width` column may follow `wire_name`:
```
wire_name,bit_width,src,dst,comment
w_bus,8,TOP/u_a.out,TOP/u_b.in,8-bit bus
```
- An **endpoint** is `HIER/PATH.port_name`. The path is the hierarchy path to
  the instance whose scope contains the signal; the last segment is the
  instance name. Top-level signals use just the top name: `TOP.my_clk`.
- A **source** signal must already exist — a missing source is a fatal error.
  Missing **destination** ports are created by the tool.
- A destination is an input of the instance named by its path or, when the
  route ends in a module's own scope (e.g. `TOP.dbg_out`), an output of that
  module, which the tool drives. Each destination takes one source, and a
  wire_name must not collide with an existing signal; violations are reported
  before anything is written.

See [`example_connections.csv`](example_connections.csv) for a worked example.

## Limitations
- Bit widths are resolved per module *definition* (a representative elaborated
  instance); a module instantiated with divergent parameter widths uses a
  single width in the rewrite.
- Write-back is text-based and only manages content inside its own `AUTO_WIRE`
  markers, inline `// aw:` stamps, and `/*aw-orig:…*/` markers (these keep the
  original text of a connection the tool rewrote, so it can be restored).
- Files keep their encoding (UTF-8, Big5/cp950, …) and line endings; in a file
  that mixes line endings, only the lines the tool edits change. If an edit
  can't be placed safely, e.g. a module without a port list or an instance with
  an empty or positional connection list, the tool stops with a
  `WRITE-BACK ERROR` and writes no files.
- Edits go into module definitions, so rewiring an existing connection inside a
  module that is instantiated more than once is refused (it would change every
  instance).
- SystemVerilog write-back covers ordinary module headers and instantiations
  (including implicit `.name` / `.*` connections), not interfaces or packages.
