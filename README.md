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

### Driving the tool from a script or UI
```
python autowire_v3.py -d ./rtl -T TOP -c conn.csv --dry-run --json plan.json
python autowire_v3.py -d ./rtl -T TOP -c conn.csv --yes --expect-plan <plan_id> --json out.json
python autowire_v3.py -d ./rtl -T TOP --tree --json tree.json
```
- `--yes` writes without the `[Y/n]` prompt.
- `--json FILE` also writes the result as JSON: `status` (`preview`, `written`,
  `aborted`, `stale`, `nothing`, `tree`, `partial`), `connections`, `warnings`,
  `mismatches`, `llm_review`, `files` (each file that will change, with a
  unified diff), `written`, and `plan_id`. With `--tree` it holds the hierarchy
  (`tree`) and every module's ports and internal signals (`modules`). A run
  that stops on an error leaves no JSON file (an existing FILE is removed when
  the run starts) and has touched no RTL file; the message is on stdout. The
  one exception is `partial` (exit status 1): the write-back failed after it
  had started. `written` lists the files that were fully replaced and
  `incomplete` a file whose direct write was cut short (see Limitations).
- `plan_id` is a fingerprint of the files as scanned and as they will be
  written. `--expect-plan ID` writes only if the plan is still the one that was
  previewed; otherwise nothing is written and the exit status is 3.
- Independently of these options, a file that changes on disk while the tool
  is running (e.g. while it waits at the prompt) is never overwritten: the run
  writes nothing and exits with status 3.

### Optional LLM review
`--llm-review` asks an OpenAI-compatible chat API (e.g. a local gpt-oss or
Qwen served by vLLM or Ollama) to flag connections that look like design
mistakes — a clock wired to data, an active-low reset to an active-high one,
tx to tx, a comment that doesn't match the signals. The findings are advisory:
they are printed before the write-back prompt and logged under `[LLM REVIEW]`
in the warning log, and they never change or block the write-back. If the API
can't be reached or its reply can't be parsed, the run continues without it.
```
export AUTOWIRE_LLM_URL=http://llm-host:8000/v1
export AUTOWIRE_LLM_MODEL=gpt-oss-120b
export AUTOWIRE_LLM_API_KEY=...        # read only from the environment
python autowire_v3.py -d ./rtl -T TOP -c conn.csv --llm-review
```
`--llm-url` / `--llm-model` override the environment variables. Only each
connection's module and signal names, directions, widths, wire name, and CSV
comment are sent — no RTL source.

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
- The new content of every file goes to a temporary file next to it first, and
  only then is each file replaced in one step, so a failed or killed run does
  not leave a file half written. A replaced file keeps its permissions (and
  group, when possible) and a symlink keeps pointing at its file, but the file
  is a new one on disk: hard links to the old file are not updated, its owner
  becomes the user who ran the tool, and on Windows its hidden/system
  attributes are not kept. A read-only file is refused. Where a file cannot be
  replaced this way for lack of permission (a directory that allows no new
  file; on Windows, a file open in another program), it is written directly
  instead, and only such a direct write can be cut short by a failure; the
  tool then names the file as possibly incomplete. A run that is killed can
  leave `.<name>.*.awtmp` files behind; they are safe to delete.
- Edits go into module definitions, so rewiring an existing connection inside a
  module that is instantiated more than once is refused (it would change every
  instance).
- SystemVerilog write-back covers ordinary module headers and instantiations
  (including implicit `.name` / `.*` connections), not interfaces or packages.
