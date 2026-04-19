# sde-py — Internal Architecture & Implementation Guide

This document explains in detail how `sde-py` was designed and built — every file, every design decision, and how the pieces fit together.

---

## Table of Contents

1. [The Core Problem](#1-the-core-problem)
2. [High-level Architecture](#2-high-level-architecture)
3. [Communication Protocol](#3-communication-protocol)
4. [File-by-file Breakdown](#4-file-by-file-breakdown)
   - [sde/_data/node_runner.mjs](#41-sde_datanode_runnermjs)
   - [sde/env.py](#42-sdeenvpy)
   - [sde/parser.py](#43-sdeparserpy)
   - [sde/compiler.py](#44-sdecompilerpy)
   - [sde/runner.py](#45-sderunnerpy)
   - [sde/result.py](#46-sderesultpy)
   - [sde/model.py](#47-sdemodelpy)
   - [sde/__init__.py](#48-sdeinitpy)
   - [pyproject.toml](#49-pyprojecttoml)
5. [Data Flow: From .mdl to Python](#5-data-flow-from-mdl-to-python)
6. [Key Design Decisions](#6-key-design-decisions)
7. [Windows-specific Considerations](#7-windows-specific-considerations)
8. [Caching Strategy](#8-caching-strategy)
9. [Thread Safety](#9-thread-safety)
10. [How SDEverywhere Works Internally](#10-how-sdeverywhere-works-internally)

---

## 1. The Core Problem

**Vensim** is a system dynamics modelling tool that uses `.mdl` files. Python has no native way to execute these models with full fidelity.

Options considered:

| Option | Problem |
|--------|---------|
| **PySD** | Re-implements the Vensim interpreter in Python — not all functions supported, results can differ from Vensim |
| **Call Vensim directly** | Requires a paid Vensim licence installed on the machine |
| **SDEverywhere (SDE)** | Compiles `.mdl` to JavaScript via a proper transpiler — same semantics as Vensim, runs anywhere with Node.js |

We chose **SDEverywhere** because:
- It transpiles to JavaScript using the same numeric algorithms as Vensim
- The compiled JS is self-contained (no runtime Vensim needed)
- The `@sdeverywhere/runtime` package provides a clean API to drive runs

The challenge: SDE is a JavaScript ecosystem. This library bridges it to Python.

---

## 2. High-level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Python process                                              │
│                                                              │
│  User code                                                   │
│     │                                                        │
│     ▼                                                        │
│  sde.Model  ──parse──►  parser.py  (reads .mdl metadata)    │
│     │                                                        │
│     ├──compile──►  compiler.py  (generates sde.config.js,   │
│     │                            runs `sde bundle`)          │
│     │                                                        │
│     └──spawn──►  runner.py  ──stdin/stdout──►  Node.js      │
│                                                    │         │
│                  result.py  ◄──JSON response───────┘         │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

The Node.js process is **persistent** — it starts once when `Model` is created and stays alive for the lifetime of the Python object. Every `m.run()` call sends one JSON line to Node and receives one JSON line back.

---

## 3. Communication Protocol

The Python↔Node bridge uses **Newline-Delimited JSON (NDJSON)** over stdio.

### Startup handshake

After Node loads the compiled model, it writes one line to stdout:

```json
{"ready": true, "startTime": 2000, "endTime": 2100, "saveFreq": 1, "outputVarIds": ["_total_inventory"]}
```

Python's `NodeRunner.spawn()` reads this line and stores it as metadata.

### Run request (Python → Node, stdin)

```json
{"inputs": [1.0, 2020.0, 30.0]}
```

The array order matches the order inputs were declared in `sde.config.js` (which comes from the parsed order in the `.mdl` file).

### Run response (Node → Python, stdout)

```json
{
  "time": [2000, 2001, ..., 2100],
  "outputs": {
    "_total_inventory": [1000, 1000, ..., 2080]
  }
}
```

### Error response

```json
{"error": "description of what went wrong"}
```

Python raises `RuntimeError` when it receives this.

### Why NDJSON over stdio?

- **Zero dependencies** — no HTTP server, no sockets, no ports to manage
- **Synchronous and ordered** — one request, one response, deterministic
- **Cross-platform** — works identically on Windows, macOS, Linux
- **Easy to debug** — you can manually pipe JSON to the Node process in a terminal

---

## 4. File-by-file Breakdown

### 4.1 `sde/_data/node_runner.mjs`

This is the Node.js bridge script. It is an **ES module** (`.mjs` extension) because `@sdeverywhere/runtime` only ships as ESM.

**What it does:**

1. Reads the path to `generated-model.js` from `process.argv[2]`
2. Dynamic-imports the generated model file:
   ```js
   const { default: loadGeneratedModel } = await import(modelUrl)
   const genModel = await loadGeneratedModel()
   ```
   Note: `pathToFileURL()` is required on Windows because Node ESM cannot import via absolute Windows paths like `C:\...` — it needs a `file:///C:/...` URL.
3. Creates a synchronous runner:
   ```js
   const runner = createSynchronousModelRunner(genModel)
   ```
4. Builds metadata from a sample outputs object and writes the ready signal
5. Enters a `readline` loop reading from stdin
6. For each input line: parses JSON, calls `runner.runModelSync(inputs, outputs)`, extracts time and output arrays, writes response JSON

**Key SDE API used:**
- `createSynchronousModelRunner(genModel)` — from `@sdeverywhere/runtime`
- `runner.createOutputs()` — creates a fresh outputs container
- `runner.runModelSync(inputValues, outputs)` — runs one simulation
- `outputs.varSeries[i].points` — array of `{x: time, y: value}` objects

**Why a separate `.mjs` file instead of inline Node code?**

The script is non-trivial (readline loop, error handling, ESM loading). Embedding it as a Python string would be unmaintainable. Shipping it as a data file and copying it to `~/.sde_env/` on setup keeps the Node code in its natural form with proper syntax highlighting and linting.

---

### 4.2 `sde/env.py`

Manages the **global npm environment** at `~/.sde_env/`.

**Key functions:**

#### `setup(verbose=True)`

Idempotent setup function. Steps:
1. Creates `~/.sde_env/` if it doesn't exist
2. Writes `package.json` declaring the four SDE packages as dependencies
3. Runs `npm install --prefer-offline` with `shell=True` (required on Windows — `npm` is a `.cmd` batch file)
4. Copies `node_runner.mjs` from the package's `_data/` directory to `~/.sde_env/`

**Why `shell=True` for npm?**

On Windows, `npm` is not a real executable — it's `npm.cmd`, a batch script. Python's `subprocess.Popen` on Windows can only launch `.exe` files directly. Passing `shell=True` routes the command through `cmd.exe`, which knows about `.cmd` files.

#### `sde_cli_path() → Path`

Locates the SDE CLI entry point (`sde.js`) inside `node_modules/@sdeverywhere/cli/`. 

This function is defensive because the CLI package structure could differ between versions:
1. First tries the well-known path `node_modules/@sdeverywhere/cli/bin/sde.js`
2. Falls back to reading `package.json` → `bin` field
3. Falls back to walking the `bin/` directory for any `.js` file

**Why not use `.bin/sde`?**

The `.bin/sde` file in `node_modules/.bin/` is a **Unix bash script shim** generated by npm. It starts with `#!/bin/sh` and uses POSIX shell syntax — it cannot be executed on Windows by Node. We always call the underlying `.js` file directly via `node sde.js`.

---

### 4.3 `sde/parser.py`

Parses a raw `.mdl` file into structured Python metadata without requiring any external tools.
The implementation is modelled directly on SDEverywhere's own preprocessor
(`packages/parse/src/vensim/preprocess-vensim.ts`).

**How `.mdl` files are structured:**

A Vensim `.mdl` file is a sequence of variable definitions separated by `|` characters. Each block looks like:

```
Variable name = equation
  ~ units [min,max,step]
  ~ documentation
  |
```

The `~` characters separate: equation part | units/range part | documentation part.

**Parsing pipeline (mirrors SDE's `preprocessVensimModel`):**

1. `_strip_encoding_header(text)` — removes `{UTF-8}` at top of file
2. Stop at `\---///` — everything after the sketch section is discarded
3. `_remove_macros(text)` — strips `:MACRO:…:END OF MACRO:` blocks
4. `_split_on_pipe(text)` — splits on `|` (Vensim block separator) **while respecting double-quoted variable names**. Quoted names can legally contain `|` (e.g. `"quotes 3 with | pipe character "`). A naive `text.split("|")` would break such names into spurious blocks. `_split_on_pipe` walks the text character-by-character tracking quote nesting and only splits on unquoted `|`.
5. For each block:
   - `_strip_inline_comments(block)` — removes `{…}` inline comments
   - `_process_backslashes(block)` — joins lines ending with `\` continuation
   - Split on `~` to get equation / units / doc parts
   - **`_reduce_whitespace(part)`** — collapses all whitespace (including `\n`, `\t`) to a single space on each part before storing
   - Extract variable name and RHS from the equation part
   - Uses regex `[min,max]` or `[min,max,step]` in units to detect **input variables** (sliders)
   - Tries `float(rhs)` to detect **numeric constants**
   - Everything else is an **output** (computed variable)
   - Time-control variables (`INITIAL TIME`, `FINAL TIME`, `TIME STEP`, `SAVEPER`) are extracted separately

**Dimension collection (`_collect_dimension_names`):**

Returns a tuple `(all_names: set, root_names: set)`. The distinction matters because SDE can only resolve subscripted variables in `spec.json` when the subscript is a *root* dimension name:

- **`all_names`**: every name appearing as a dimension definition (`DimA: A1, A2, A3 ~~|`)
- **`root_names`**: only dimensions whose elements are *not* themselves dimension names AND which are not a strict sub-range of another dimension

Sub-range detection: `SubA: A1, A2` is excluded from `root_names` when its elements are a proper subset of another dim's elements (`DimA: A1, A2, A3`).

Hierarchical detection: `DimAB: DimAB1, DimAB2` is excluded from `root_names` when any element is itself a known dimension name.

The function strips tilde-separated trailing parts (e.g. `~~|` compact notation) before parsing element names to avoid contamination.

**`_parse_block` — `hasExcept` flag:**

Variables using `:EXCEPT:` (`k[DimA] :EXCEPT: [A1] = ...`) define only a subset of subscript elements. SDE cannot find them by bare name in `spec.json`, so `hasExcept=True` forces `allDimSubscripts=False`, excluding the variable from outputs.

**`is_numeric_array_const` detection — multi-case logic:**

A subscripted variable is excluded from `spec.json` outputs if its RHS matches any of these:

| RHS pattern | Example | Reason |
|---|---|---|
| Numeric list with comma/semicolon | `= -1, +2, 3` | Inline constant array |
| `TABBED ARRAY(...)` | `= TABBED ARRAY(\t1\t2\n...)` | Inline data table |
| `GET DIRECT LOOKUPS(...)` | `= GET DIRECT LOOKUPS('data.csv', ...)` | Lookup table from external file |
| `GET XLS LOOKUPS(...)` | `= GET XLS LOOKUPS('data.xlsx', ...)` | Lookup table from Excel |

Note: `_is_numeric_array_rhs()` requires at least one comma or semicolon. A bare single number (`ce[t1] = 1`) is an element-specific constant, not a numeric array — it remains an output.

**Why `_reduce_whitespace` matters:**

Vensim equations are written across multiple lines with tabs for readability:
```
Target Capacity= ACTIVE INITIAL (
    Capacity*Utilization Adjustment,
        Initial Target Capacity)
```
Without whitespace reduction the stored equation contains raw newlines/tabs, which breaks `m.info()` display and any downstream processing. After reduction it becomes:
```
ACTIVE INITIAL ( Capacity*Utilization Adjustment, Initial Target Capacity)
```

**Name conversion (mirrors SDE's `canonicalId`):**

`to_python_name("Production start year")` → `"production_start_year"`

Algorithm:
1. Replace one or more consecutive whitespace/underscore chars with a single `_` (matches Vensim's documented name rules)
2. Replace any remaining non-alphanumeric characters with `_`
3. Collapse multiple `_`, strip leading/trailing `_`, lowercase

**Important:** Most `.mdl` files do **not** have `[min,max]` annotations — these are a Vensim UI slider convention and are absent from pure simulation models. The `inputs` list will be empty for such models. After parsing, `_merge_inputs()` folds in any user-declared inputs, and then `_auto_promote_constants()` automatically promotes every remaining constant to an input. This means you can always call `m.run(some_constant=value)` without having to know or declare any constants upfront.

**What the parser returns:**

```python
{
  "inputs": [
    {"varName": "Production slope", "pythonName": "production_slope",
     "defaultValue": 1.0, "minValue": 1.0, "maxValue": 10.0, "stepValue": None, "doc": ""},
    ...
  ],
  "outputs": [
    {"varName": "Total inventory", "pythonName": "total_inventory",
     "equation": "Initial inventory + RAMP(Production slope, ...)", "doc": ""},
    # equation is always a single-line string (whitespace already reduced)
  ],
  "constants": [
    {"varName": "Initial inventory", "pythonName": "initial_inventory", "value": 1000.0},
  ],
  "time": {"initial": 2000.0, "final": 2100.0, "step": 1.0, "saveper": 1.0},
  "pyToVensim": {"production_slope": "Production slope", ...},
  "vensimToPy": {"Production slope": "production_slope", ...},
}
```

---

### 4.4 `sde/compiler.py`

Compiles a `.mdl` file into `generated-model.js` using the SDE CLI.

**Step 1: Hash check**

```python
current_hash = hashlib.md5(mdl_path.read_bytes()).hexdigest()
```

If the hash matches the one stored in `cache_dir/model-hash.txt` and `generated-model.js` already exists → skip compilation. This means repeated `sde.Model(...)` calls on an unchanged file take milliseconds instead of ~10 seconds.

**Step 2: Set up compile workspace**

The compilation does NOT happen in `cache_dir`. It happens in `~/.sde_env/compile-workspace/`.

**Why?**

Node.js ESM (the module system used by `sde.config.js`) resolves imports by walking up the directory tree looking for `node_modules/`. It does **not** respect the `NODE_PATH` environment variable for ESM imports. Since `@sdeverywhere/plugin-worker` (imported in `sde.config.js`) lives in `~/.sde_env/node_modules/`, the config file must be run from a directory that is inside `~/.sde_env/` so that walking up finds `node_modules/`.

**Step 3: Generate `sde.config.js`**

`_generate_sde_config()` programmatically writes an `sde.config.js` file. The generated config:
- Sets `modelFiles` to the absolute path of the `.mdl` file (as a POSIX string for Node compatibility)
- Declares all parsed inputs with their min/max/default
- Declares all parsed outputs
- Includes `workerPlugin()` to produce `generated-model.js`

**Step 4: Run `sde bundle`**

```python
subprocess.run(
    f'node "{cli}" bundle --config sde.config.js',
    cwd=compile_ws,
    shell=True,
)
```

Using `shell=True` because on Windows, even `node` sometimes needs it depending on PATH configuration. The full CLI path is quoted to handle spaces in paths like `C:\Users\Pat UK\`.

**Step 5: Copy output**

`sde bundle` writes `sde-prep/generated-model.js` inside the compile workspace. We copy this to `cache_dir/generated-model.js` and write the MD5 hash to `cache_dir/model-hash.txt`.

---

### 4.5 `sde/runner.py`

Manages the persistent Node.js subprocess.

**`NodeRunner.spawn(generated_model_path) → NodeRunner`**

Starts the process:

```python
proc = subprocess.Popen(
    ["node", str(runner_script), str(generated_model_path)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,  # line-buffered
)
```

`bufsize=1` is critical — it enables line-buffering on the text stream so that Python's `readline()` returns as soon as Node writes a complete line, not only when the buffer is full.

After spawning, it reads the startup JSON and stores it as `self.metadata`.

**`NodeRunner.run(inputs) → dict`**

```python
def run(self, inputs):
    cmd = json.dumps({"inputs": [float(v) for v in inputs]}) + "\n"
    with self._lock:
        self._proc.stdin.write(cmd)
        self._proc.stdin.flush()
        response_line = self._proc.stdout.readline()
    return json.loads(response_line)
```

The `_lock` (a `threading.Lock`) ensures that if multiple Python threads call `run()` concurrently, the stdin writes and stdout reads don't interleave. Only one run executes at a time; others queue up.

**Why not use `asyncio` or `multiprocessing`?**

The model run itself is synchronous and very fast (sub-millisecond). The overhead of async machinery or process spawning per call would dominate. A simple lock-protected synchronous protocol is the right fit.

---

### 4.6 `sde/result.py`

Wraps the raw JSON output dictionary into a user-friendly object.

**Construction:**

```python
ModelResult(raw, meta, derived_fns)
```

- `raw` — `{"time": [...], "outputs": {"_total_inventory": [...]}}`
- `meta` — from `parser.py` (contains `vensimToPy` mapping, time bounds, etc.)
- `derived_fns` — dict of `name → callable` registered via `model.derive()`

During `__init__`, each output array is wrapped in a `pandas.Series` indexed by the raw time list. The same series is registered under both the SDE var-id (`_total_inventory`) and the stripped name (`total_inventory`).

Derived variables are computed immediately during construction by calling each registered function with `self` (the partial result). This means derived variables are always consistent with the run they come from.

**`at(time_value)` implementation:**

```python
pos = int((self._time - time_value).abs().argmin())
return {k: float(v.iloc[pos]) for k, v in self._series.items()}
```

`argmin()` returns a **positional index** (0, 1, 2, ...). We then use `iloc[pos]` to index by position. This is correct regardless of what the Series index is.

Earlier versions used `idxmin()` + `loc[idx]`, which failed because `idxmin()` returns the **label** of the minimum (e.g., `50` = the 50th element, year 2050 when start=2000), but the output series were indexed by time values (2000, 2001, ...) — so `loc[50]` looked for label `50`, not position `50`.

**Attribute access via `__getattr__`:**

```python
def __getattr__(self, name):
    if name.startswith("_"):
        raise AttributeError(name)
    try:
        return self._resolve(name)
    except KeyError as e:
        raise AttributeError(str(e)) from e
```

Python calls `__getattr__` only when normal attribute lookup fails. The `if name.startswith("_")` guard is essential — without it, Python internals (like pickle, copy, deepcopy) that probe for dunder attributes would enter infinite recursion.

---

### 4.7 `sde/model.py`

The main public-facing class that orchestrates all the pieces.

**`__init__(mdl_path, inputs=None, verbose=True, force_recompile=False, cache_dir=None)`**

The `inputs` parameter accepts a list of explicit input declarations.  Each entry is either:
- A **dict** with at minimum `varName` and optionally `default`, `min`, `max`
- A **string** (just the Vensim variable name); `min=0`, `max=100`, `default` taken from the constants table

Examples:
```python
sde.Model("active_initial.mdl", inputs=[
    {"varName": "Capacity Adjustment Time", "default": 10, "min": 1, "max": 50},
    "Utilization Sensitivity",
])
```

`__init__` calls `_load_and_compile()` which:
1. Calls `require_setup()` to fail early with a helpful message if setup hasn't been run
2. Calls `parse_mdl()` to get metadata
3. Calls `_merge_inputs(meta, self._extra_inputs)` to fold in user-declared inputs
4. Calls `_auto_promote_constants(meta)` to promote any remaining constants to inputs
5. Calls `compile_mdl()` to get `generated-model.js`
6. Calls `NodeRunner.spawn()` to start the Node process
7. Merges time metadata from the runner's ready message (which comes from the compiled model, so it's authoritative) with the parsed metadata

**`_merge_inputs(meta, extra)` (module-level helper):**

Merges user-declared inputs into `meta["inputs"]` in-place:
- Looks up the default value from `meta["constants"]` for each declared variable
- Removes promoted variables from `meta["constants"]` so they don't appear in both lists
- Updates the `pyToVensim` / `vensimToPy` name-mapping dicts
- Variables already present in `meta["inputs"]` (from `[min,max]` mdl annotations) are skipped
- `self._extra_inputs` is stored on the instance so `load()` and `reload()` re-apply it automatically

**`_auto_promote_constants(meta)` (module-level helper):**

Called after `_merge_inputs`. Promotes every constant still in `meta["constants"]` into `meta["inputs"]`:
- Default value = the `.mdl` constant value
- `min=0.0`, `max=100.0` (safe fallbacks when no range is known)
- `meta["constants"]` is cleared to `[]` afterwards

This means `m.run(some_constant=value)` always works, even if the user passed no `inputs=` at all and never opened the `.mdl` file.

**`run(**kwargs)`:**

1. Calls `_build_inputs(**kwargs)` to construct the ordered input list
2. Calls `self._runner.run(inputs)` → gets raw JSON dict
3. Constructs and returns `ModelResult(raw, self._meta, self._derived_fns)`

**`_build_inputs(**kwargs)`:**

```python
defaults = {v["pythonName"]: v["defaultValue"] for v in meta_inputs}
for key, val in kwargs.items():
    if key not in defaults:
        raise ValueError(f"Unknown input: '{key}'. Available: {list(defaults.keys())}")
    defaults[key] = val
return [defaults[v["pythonName"]] for v in meta_inputs]
```

This starts from the full default dict, applies overrides, then serialises in the **same order as the parsed inputs list** — which matches the order `sde.config.js` was generated with.

**`sensitivity(param, values, **fixed_kwargs)`:**

Runs the model `len(values)` times, collecting the first output variable for each run, and assembles a DataFrame. The "first output variable" heuristic is intentional — for single-output models this is always correct. Multi-output support could be added by returning a dict of DataFrames.

**`load(new_mdl_path)` and `reload()`:**

Both call `_load_and_compile()` again. The existing runner is terminated first:

```python
if hasattr(self, "_runner") and self._runner is not None:
    self._runner.terminate()
```

`hasattr` is used because `_load_and_compile` is called from `__init__` before `_runner` exists. `self._extra_inputs` is preserved across both calls, so user-declared inputs are re-applied after hot-swap.

---

### 4.8 `sde/__init__.py`

The public API surface:

```python
from .env import setup, is_setup, env_path
from .model import Model

__all__ = ["Model", "setup", "is_setup", "env_path"]
```

Intentionally minimal. Users only need `Model` and `setup`. The internal modules (`parser`, `compiler`, `runner`, `result`) are not exported — they're implementation details.

---

### 4.9 `pyproject.toml`

Defines the installable package:

```toml
[build-system]
requires = ["setuptools>=42"]
build-backend = "setuptools.build_meta"

[project.scripts]
sde-setup = "sde.env:setup_cli"

[tool.setuptools.package-data]
sde = ["_data/*.mjs"]
```

**`build-backend = "setuptools.build_meta"`** — the standard setuptools backend, available since setuptools 42. The alternative `setuptools.backends.legacy:build` was introduced only in setuptools 69+ and causes `BackendUnavailable` errors on older installations.

The `package-data` entry is critical — it tells setuptools to include `node_runner.mjs` when the package is installed (or installed in editable mode). Without it, `_data/` would not be shipped.

The `sde-setup` console script entry point calls `setup_cli()` in `env.py`, which just calls `setup(verbose=True)`. This lets users run `sde-setup` from any terminal after pip-installing the package.

---

## 5. Data Flow: From .mdl to Python

```
model.mdl
    │
    ▼
parser.py: parse_mdl()
    │                                  (equations stored as single-line
    ├── meta["inputs"]   → list of {varName, pythonName, default, min, max}   strings — whitespace reduced)
    ├── meta["outputs"]  → list of {varName, pythonName, equation}
    ├── meta["constants"]→ list of {varName, pythonName, value}
    ├── meta["time"]     → {initial, final, step, saveper}
    └── meta["pyToVensim"], meta["vensimToPy"]
    │
    ▼
model.py: _merge_inputs(meta, extra_inputs)    ← user-declared inputs folded in here
    │                                             constants promoted to inputs are
    │                                             removed from meta["constants"]
    ▼
model.py: _auto_promote_constants(meta)        ← remaining constants auto-promoted
    │                                             meta["constants"] becomes []
    ▼
compiler.py: compile_mdl()
    │
    ├── writes ~/.sde_env/compile-workspace/sde.config.js
    ├── runs:  node sde.js bundle --config sde.config.js
    │              (in ~/.sde_env/compile-workspace/)
    │              → creates sde-prep/generated-model.js
    └── copies generated-model.js → cache_dir/generated-model.js
    │
    ▼
runner.py: NodeRunner.spawn()
    │
    ├── starts: node node_runner.mjs cache_dir/generated-model.js
    ├── reads ready JSON from stdout
    └── stores NodeRunner instance
    │
    ▼
model.py: Model instance ready

    User calls: m.run(production_slope=5)
    │
    ▼
model.py: _build_inputs() → [5.0, 2020.0, 30.0]
    │
    ▼
runner.py: NodeRunner.run([5.0, 2020.0, 30.0])
    │
    ├── writes: {"inputs": [5.0, 2020.0, 30.0]}\n  to Node stdin
    └── reads:  {"time": [...], "outputs": {"_total_inventory": [...]}}\n  from Node stdout
    │
    ▼
result.py: ModelResult(raw, meta, derived_fns)
    │
    └── returned to user as pandas-backed result object
```

---

## 6. Key Design Decisions

### Decision 1: Persistent Node process instead of spawning per run

Spawning a Node process costs ~300ms on Windows. A single model run takes ~1ms. If we spawned per run, overhead would be 300× the actual work. The persistent process approach brings this overhead to effectively zero after the first spawn.

### Decision 2: Synchronous runner instead of async

`@sdeverywhere/runtime` provides both a synchronous runner (`createSynchronousModelRunner`) and an async runner (used by the browser web worker). The synchronous runner is simpler — no Promises, no await, no event loop. Since Python is driving the process and already serialises calls via a lock, synchronous is the right choice.

### Decision 3: Auto-generate `sde.config.js` instead of requiring users to write one

A core design goal was: "place a `.mdl` file and pass it to a library function." Requiring users to manually write `sde.config.js` (a Node.js config file) would break this. The parser extracts enough information to generate a complete config automatically.

### Decision 6: All constants become inputs automatically

Most `.mdl` files have no `[min,max]` slider annotations — that convention is only used by models designed for Vensim's GUI. SDE itself does not read `[min,max]` either; inputs are declared independently in `sde.config.js`.

The pipeline is:
1. Parser reads `[min,max]` as a convenience for models that already have them
2. `_merge_inputs()` folds in any explicit `inputs=[...]` the user provided (with custom min/max)
3. `_auto_promote_constants()` promotes every remaining constant with `min=0, max=100`

The result: `meta["constants"]` is always empty after load, and every constant in the model is reachable via `m.run(constant_name=value)` without the user needing to read the `.mdl` file. Explicit `inputs=[...]` is only needed when you want non-default min/max ranges.

### Decision 4: MD5 hash-based caching

Compilation is slow (~10s). Users typically load the same model repeatedly (in a Jupyter notebook, every kernel restart). Without caching, every `sde.Model(...)` call would recompile. The hash check makes repeated loads instant.

### Decision 5: Separate compile workspace vs. cache directory

The cache directory holds the compiled output and is keyed by model path. The compile workspace is always `~/.sde_env/compile-workspace/` — a single shared location. This is a trade-off: parallel compilation of two different models would conflict. In practice this is not an issue since compilation is a one-time cost per model.

---

## 7. Windows-specific Considerations

Several Windows-specific issues were encountered and fixed during development:

| Issue | Root Cause | Fix |
|-------|------------|-----|
| `FileNotFoundError` on `npm install` | `npm` is `npm.cmd`, not an EXE | `shell=True` in subprocess calls |
| `SyntaxError` running `.bin/sde` | `.bin/sde` is a bash script shim | Use `node sde.js` directly, never `.bin/sde` |
| `ERR_MODULE_NOT_FOUND` in sde.config.js | Node ESM ignores `NODE_PATH` | Run compilation from inside `~/.sde_env/` |
| `pathToFileURL()` needed in node_runner.mjs | Node ESM cannot import `C:\...` paths | Convert to `file:///C:/...` URL first |

---

## 8. Caching Strategy

```
~/.sde_env/
├── package.json            ← npm dependencies declaration
├── node_modules/           ← installed npm packages (shared by all models)
├── node_runner.mjs         ← bridge script (copied from sde/_data/)
└── compile-workspace/      ← where sde bundle is run
    ├── package.json
    ├── sde.config.js       ← auto-generated for current model
    └── sde-prep/
        └── generated-model.js

~/.sde_env/model-cache/
└── e__RND_finale_hello_world_model_sample_mdl_/   ← keyed by model path
    ├── generated-model.js   ← compiled output (copied here after bundle)
    ├── model-hash.txt       ← MD5 of the .mdl file at last compile
    └── package.json
```

The cache key is derived from the full absolute path of the `.mdl` file, sanitised to a safe directory name (non-alphanumeric chars → `_`, truncated to 80 chars).

---

## 9. Thread Safety

`NodeRunner` uses a `threading.Lock` (`self._lock`) around every stdin write + stdout read pair.

This means:
- Multiple Python threads can hold a reference to the same `Model` and call `m.run()` concurrently
- Each call will complete correctly, but they will be **serialised** (queued) rather than truly parallel
- True parallelism would require multiple Node processes — one per thread — which is a potential future enhancement

`ModelResult` objects are immutable after construction — they are safe to share across threads without locks.

---

## 10. How SDEverywhere Works Internally

Understanding SDE helps explain several design choices.

### SDE compilation pipeline

1. **Parse**: `sde.js` reads the `.mdl` file using its own Vensim MDL parser
2. **Translate**: Each Vensim equation is translated to a JavaScript function
3. **Bundle**: `sde bundle` (which uses `@sdeverywhere/plugin-worker`) produces `generated-model.js` — an ES module with a default export that is an async factory function

### The generated model API

`generated-model.js` exports:

```js
export default async function() {
  return {
    getInitialTime: () => 2000,
    getFinalTime: () => 2100,
    getTimeStep: () => 1,
    getSaveFreq: () => 1,
    setInputs: (inputValues) => { ... },   // sets the input array
    storeOutputs: (outputValues) => { ... },
    initConstants: () => { ... },
    initLevels: () => { ... },
    evalAux: () => { ... },
    evalLevels: () => { ... },
  }
}
```

### What `createSynchronousModelRunner` does

The `@sdeverywhere/runtime` package wraps the raw model object in a runner that:
1. Calls `initConstants()` and `initLevels()` to set initial state
2. Loops from `initialTime` to `finalTime` in steps of `timeStep`
3. At each step: calls `setInputs(values)`, `evalAux()`, `evalLevels()`, `storeOutputs()`
4. Collects output values into `varSeries` arrays

This is the standard Euler integration loop used by Vensim's own engine.

### Why the inputs must be in a specific order

`setInputs(values)` takes a plain array — there are no names, just positions. The position mapping is determined at compile time by the order inputs appear in `sde.config.js`. This is why the parser preserves insertion order (Python dicts are ordered since 3.7) and `_build_inputs()` serialises in the same order as `meta["inputs"]`.
