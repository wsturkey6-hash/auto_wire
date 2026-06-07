# AutoWire — CSV-driven SoC point-to-point wiring

AutoWire reads a folder of Verilog RTL and a CSV of point-to-point connections,
then automatically threads each connection through the module hierarchy —
adding the ports, wires, `assign`s, and instantiation connections needed at
every level — and writes the changes back into the `.v` files inside clearly
marked, idempotent `// AUTO_WIRE_BEGIN … // AUTO_WIRE_END` blocks.

## How it works
1. **Parse** all `.v` files with [pyslang](https://github.com/MikePopoloski/slang)
   (a real SystemVerilog/Verilog frontend) — accurate ports, directions, and
   concrete (parameter-resolved) bit widths.
2. **Elaborate** the design from the given top module to build the instance
   hierarchy.
3. For each connection, compute the **lowest common ancestor (LCA)** of the
   source and destination, "bubble" the signal up to the LCA and back down to
   the destination, inserting ports/wires/assigns/connections at each level.
4. **Write back** the edits idempotently — re-running reproduces the same
   result, because the tool strips its own previous output first.

## Install
```
pip install -r requirements.txt
```
Requires Python 3 and `pyslang`. Optionally install Icarus Verilog (`iverilog`)
or Verilator to enable the test suite's external elaboration/lint cross-check,
and `openpyxl` if your connection list is an `.xlsx` file.

## Usage
```
python autowire_v3.py --rtl-dir ./rtl --top TOP --csv connections.csv
python autowire_v3.py -d ./rtl -T TOP -c conn.csv --dry-run   # preview only
python autowire_v3.py -d ./rtl -T TOP -c conn.csv --tree      # print hierarchy
```

## Connection CSV format
Comment lines start with `#`; blank lines are ignored.
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

See [`example_connections.csv`](example_connections.csv) for a worked example.

## Limitations
- Bit widths are resolved per module *definition* (a representative elaborated
  instance); a module instantiated with divergent parameter widths uses a
  single width in the rewrite.
- Write-back is text-based and only manages content inside its own `AUTO_WIRE`
  markers / inline `// aw:` stamps.
