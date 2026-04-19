# sde-py — Complete User Tutorial

`sde-py` is a Python library that lets you load, compile, and run **Vensim system dynamics models** (`.mdl` files) from Python. Under the hood it uses [SDEverywhere](https://github.com/climateinteractive/SDEverywhere) to compile the model to JavaScript, then drives it from Python via a persistent Node.js subprocess.

---

## Table of Contents

1. [Installation](#1-installation)
2. [First-time Setup](#2-first-time-setup)
3. [Loading a Model](#3-loading-a-model)
4. [Declaring Inputs](#4-declaring-inputs)
5. [Inspecting a Model](#5-inspecting-a-model)
6. [Running the Model](#6-running-the-model)
7. [Working with Results](#7-working-with-results)
8. [Point-in-time Lookup](#8-point-in-time-lookup)
9. [Derived Variables](#9-derived-variables)
10. [Sensitivity Analysis](#10-sensitivity-analysis)
11. [Hot-swapping / Reloading the Model](#11-hot-swapping--reloading-the-model)
12. [Context Manager Usage](#12-context-manager-usage)
13. [Full End-to-End Example](#13-full-end-to-end-example)
14. [API Reference](#14-api-reference)
15. [Common Errors & Fixes](#15-common-errors--fixes)

---

## 1. Installation

### Requirements
- Python 3.9+
- Node.js 18+ (must be on your PATH)
- npm (comes with Node.js)

### Install the library

```bash
pip install -e "e:\RND finale\sde-py"
```

The `-e` flag installs in **editable mode** — any changes you make to the library files take effect immediately without reinstalling.

### Install optional plotting dependency

```bash
pip install matplotlib
```

---

## 2. First-time Setup

Before using any model, you must run setup **once**. This downloads the SDEverywhere npm packages (~50 MB) into a shared global folder at `~/.sde_env/`.

```python
import sde

sde.setup()
```

**Output:**
```
Installing SDEverywhere npm packages into ~/.sde_env/ ...
added 312 packages in 28s
✓ Setup complete. SDEverywhere is ready.
```

### What setup does
- Creates `~/.sde_env/` directory
- Writes a `package.json` declaring `@sdeverywhere/cli`, `@sdeverywhere/runtime`, `@sdeverywhere/build`, `@sdeverywhere/plugin-worker` as dependencies
- Runs `npm install` to download them
- Copies `node_runner.mjs` (the bridge script) into `~/.sde_env/`

### Checking if setup has been done

```python
print(sde.is_setup())   # True or False
print(sde.env_path())   # Path to ~/.sde_env
```

### Running setup from the command line

After installing, a console script is available:

```bash
sde-setup
```

This is equivalent to calling `sde.setup()` from Python.

---

## 3. Loading a Model

```python
import sde

m = sde.Model(r"path\to\your\model.mdl")
```

### What happens on load
1. The `.mdl` file is **parsed** — inputs (from `[min,max]` annotations), outputs, constants, and time parameters are extracted
2. Any **user-declared inputs** are merged in (see section 4)
3. **All remaining constants are automatically promoted to inputs** — so every constant is always controllable via `m.run()` without any configuration
4. The model is **compiled** using `sde bundle` → produces `generated-model.js`
5. A **Node.js subprocess** is spawned and kept alive for fast subsequent runs
6. An MD5 hash of the `.mdl` file is cached — if you load the same unchanged file again, compilation is skipped automatically

### Options

```python
m = sde.Model(
    mdl_path="model.mdl",
    inputs=None,            # explicit input declarations (see section 4)
    verbose=True,           # print progress (default: True)
    force_recompile=False,  # skip cache check and always recompile (default: False)
    cache_dir=None,         # custom cache directory (default: ~/.sde_env/model-cache/<hash>/)
)
```

### Silently loading (no output)

```python
m = sde.Model("model.mdl", verbose=False)
```

---

## 4. Declaring Inputs

**You don't need to do anything.** Every numeric constant in the model is automatically available as an input to `m.run()`. The default value is taken from the `.mdl` file.

```python
# Just load and run — constants are inputs automatically
m = sde.Model(r"E:\RND finale\SDEverywhere\models\active_initial\active_initial.mdl")

m.run()                               # all defaults from .mdl
m.run(capacity_adjustment_time=20)    # override one
m.run(initial_target_capacity=200, utilization_sensitivity=2)
```

Call `m.info()` to see what inputs are available and their default values.

### Custom min/max ranges (optional)

By default, auto-promoted constants get `min=0, max=100` as a safe fallback. If you need tighter or wider bounds (e.g., for a sweep or a UI slider), use the `inputs=` parameter:

```python
m = sde.Model(
    "active_initial.mdl",
    inputs=[
        {"varName": "Capacity Adjustment Time", "min": 1, "max": 50},
        {"varName": "Initial Target Capacity",  "min": 10, "max": 500},
    ]
)
# Utilization Sensitivity is not listed above, so it gets auto-promoted with min=0, max=100
```

### String shorthand

Pass just the Vensim variable name. Min/max default to `0`/`100`; default value is taken from the `.mdl` constant.

```python
m = sde.Model(
    "active_initial.mdl",
    inputs=["Capacity Adjustment Time", "Initial Target Capacity"]
)
```

### What types of variables can be inputs?

| Variable type | Can be input? | Why |
|---|---|---|
| **Constant** (plain number) | ✅ Yes | Gets overridden once before run; all equations that reference it use the new value |
| **Auxiliary** (equation) | ❌ No | Recomputed every timestep — your value would be overwritten immediately |
| **INTEG (stock)** | ❌ Not directly | Its initial-value *constant* can be made an input |

### The compiled input set is fixed at creation time

The full set of inputs is baked into `sde.config.js` at compilation time. Since all constants are promoted automatically, this is never a limitation in practice. The *values* passed to `m.run()` can always vary freely.

---

## 5. Inspecting a Model

```python
m.info()
```

**Example output (sample.mdl — inputs from `[min,max]` annotations):**
```
Model: sample.mdl
  Time: 2000.0 → 2100.0 (step=1.0)

  Inputs:
    'production_slope'               [1.0, 10.0]  default=1.0
    'production_start_year'          [2020.0, 2070.0]  default=2020.0
    'production_years'               [0.0, 30.0]  default=30.0

  Outputs:
    'total_inventory'                Initial inventory+RAMP(Production slope, ...

  Constants:
    'initial_inventory'              = 1000.0
```

**Example output (active_initial.mdl — no `[min,max]` annotations, no `inputs=` passed):**
```
Model: active_initial.mdl
  Time: 0.0 → 100.0 (step=1.0)

  Inputs:  (auto-promoted from constants)
    'capacity_adjustment_time'       [0.0, 100.0]  default=10.0
    'initial_target_capacity'        [0.0, 100.0]  default=100.0
    'utilization_sensitivity'        [0.0, 100.0]  default=1.0

  Outputs:
    'capacity'                       INTEG ( Capacity Adjustment Rate, Target Capacity)
    'capacity_adjustment_rate'       (Target Capacity-Capacity)/Capacity Adjustment Time
    'capacity_utilization'           Production/Capacity
    'production'                     100+STEP(100,10)
    'target_capacity'                ACTIVE INITIAL ( Capacity*Utilization Adjustment, ...
    'utilization_adjustment'         Capacity Utilization^Utilization Sensitivity

  Constants:
    (empty — all promoted to inputs automatically)
```

The names shown are the **Python-style names** you use in `m.run(...)`.

---

## 5. Running the Model

### Default run (all inputs at their default values)

```python
result = m.run()
```

### Override one or more inputs

```python
result = m.run(production_slope=5)
result = m.run(production_slope=8, production_start_year=2030)
result = m.run(production_slope=3, production_start_year=2025, production_years=15)
```

- All unspecified inputs use their **default values** from the `.mdl` file
- Input names use **snake_case** (spaces replaced with underscores, all lowercase)
- Passing an unknown input name raises `ValueError` with a list of valid names

### Input name mapping

| Vensim name             | Python name              |
|-------------------------|--------------------------|
| `Production slope`      | `production_slope`       |
| `Production start year` | `production_start_year`  |
| `Production years`      | `production_years`       |
| `Initial inventory`     | `initial_inventory`      |

### Multiple runs

Each `m.run()` call is fast (milliseconds) because the Node.js process stays alive between calls.

```python
results = [m.run(production_slope=v) for v in range(1, 11)]
```

---

## 7. Working with Results

`m.run()` returns a `ModelResult` object.

### Access output variables as pandas Series

```python
result = m.run()

# By attribute (use the python snake_case name)
ts = result.capacity            # pandas.Series, index = time
ts = result.total_inventory     # for sample.mdl

# By key
ts = result["capacity"]

# List all available variable names
print(result.keys())
```

### The time axis

```python
print(result.time)          # pandas.Series of time values [2000, 2001, ..., 2100]
print(result.time.iloc[0])  # 2000.0
print(result.time.iloc[-1]) # 2100.0
```

### Inspecting the result

```python
print(result)
# <ModelResult t=2000.0→2100.0 vars=[total_inventory, _total_inventory, ...]>
```

### Using pandas methods

Since variables are `pandas.Series`, all pandas methods work:

```python
ts = result.total_inventory

ts.head(5)              # first 5 rows
ts.tail(5)              # last 5 rows
ts.describe()           # count, mean, std, min, max, ...
ts.max()                # peak value
ts.idxmax()             # time at peak
ts.diff().plot()        # rate of change
```

### Plotting

```python
import matplotlib.pyplot as plt

result.total_inventory.plot(
    title="Total Inventory",
    xlabel="Year",
    ylabel="Units"
)
plt.show()
```

### Comparing two runs

```python
r1 = m.run(production_slope=1)
r2 = m.run(production_slope=10)

import pandas as pd
pd.DataFrame({
    "slope=1":  r1.total_inventory,
    "slope=10": r2.total_inventory,
}).plot(title="Comparison")
plt.show()
```

---

## 7. Point-in-time Lookup

Get all variable values at a specific simulation time:

```python
result = m.run(production_slope=5)

snapshot = result.at(2050)
print(snapshot)
# {'total_inventory': 1180.0, '_total_inventory': 1180.0}

print(snapshot["total_inventory"])  # 1180.0
```

- Uses nearest-neighbour lookup, so fractional times work even when the step is 1
- Returns a plain Python `dict` mapping variable names → `float`

---

## 8. Derived Variables

You can register computed variables that are calculated from the model outputs after each run.

### Register a derived variable

```python
m.derive("inventory_doubled", lambda r: r.total_inventory * 2)
```

The function receives the full `ModelResult` and must return a `pandas.Series`.

### Access like any other variable

```python
result = m.run(production_slope=5)
print(result.inventory_doubled)         # pandas.Series
print(result.inventory_doubled[2060])   # value at year 2060
```

### More examples

```python
# Difference from baseline
baseline = m.run()
m.derive("delta", lambda r: r.total_inventory - baseline.total_inventory)

# Normalised (0–1 scale)
m.derive("normalised", lambda r: (
    (r.total_inventory - r.total_inventory.min()) /
    (r.total_inventory.max() - r.total_inventory.min())
))

# Rate of change (derivative)
m.derive("rate", lambda r: r.total_inventory.diff().fillna(0))

# Cumulative sum
m.derive("cumulative", lambda r: r.total_inventory.cumsum())
```

### Derived variables persist across runs

Once registered with `m.derive(...)`, the derived variable is computed for every future `m.run()` call.

```python
m.derive("doubled", lambda r: r.total_inventory * 2)

r1 = m.run(production_slope=1)
r2 = m.run(production_slope=5)

print(r1.doubled)   # available
print(r2.doubled)   # also available
```

---

## 9. Sensitivity Analysis

Run the model across a range of values for **one input** and get results as a single DataFrame.

> **Important:** The first argument to `sensitivity()` must be the Python name of an **input** variable — one shown in `m.info()` under *Inputs*. You cannot sweep an output variable (auxiliary, stock, etc.) directly, because sde-py sends the value to the model *before* the run begins, and only inputs are wired to receive pre-run values. To see how an output like `capacity` changes, sweep an input that drives it (e.g. `capacity_adjustment_time`) and observe `capacity` in the resulting DataFrame.
>
> **Common mistake:** `m.sensitivity('capacity', [...])` fails with `ValueError: Unknown input: 'capacity'` because `capacity` is an INTEG (stock) — it is an *output*, not an input. Use `m.sensitivity('capacity_adjustment_time', [...])` instead.

### Basic sweep

```python
df = m.sensitivity("production_slope", [1, 3, 5, 7, 10])
print(df)
```

Returns a `pandas.DataFrame` where:
- **Index** = simulation time (2000 … 2100)
- **Columns** = one per value, labelled `production_slope=1`, `production_slope=3`, etc.

### Plot the sweep

```python
df.plot(title="Sensitivity: Production Slope", xlabel="Year", ylabel="Total Inventory")
plt.show()
```

### Fix other inputs while sweeping

```python
df = m.sensitivity(
    "production_slope",
    [1, 3, 5, 7, 10],
    production_start_year=2040,   # held fixed for all runs
    production_years=20,
)
```

### Extract a specific year

```python
print(df.loc[2080])   # values at year 2080 for each slope
```

### Multiple sweeps

```python
sweep_slope = m.sensitivity("production_slope", [1, 5, 10])
sweep_years = m.sensitivity("production_years", [5, 15, 30])
```

---

## 10. Hot-swapping / Reloading the Model

### Load a different .mdl file

```python
m.load(r"path\to\other_model.mdl")
```

- Recompiles only if the file has changed (MD5 hash check)
- Restarts the Node.js runner automatically
- All previously registered derived variables are preserved

### Force recompile the current model

Use this after you edit the `.mdl` file externally:

```python
m.reload()
```

This ignores the hash cache and always recompiles.

---

## 11. Context Manager Usage

Use `Model` as a context manager to ensure the Node.js process is always shut down:

```python
with sde.Model("model.mdl") as m:
    result = m.run(production_slope=5)
    print(result.total_inventory.max())
# Node.js process is terminated here automatically
```

### Manual cleanup

```python
m = sde.Model("model.mdl")
result = m.run()
m.close()   # shut down the Node.js runner
```

If you forget to call `close()`, Python's garbage collector will terminate the process when `m` goes out of scope (via `__del__`), but using `close()` or the context manager is more reliable.

---

## 12. Full End-to-End Example

Two examples: one for a model with `[min,max]` annotations, one without (constants auto-promoted).

### Example A — model with slider annotations (sample.mdl)

```python
import sde
import matplotlib.pyplot as plt

sde.setup()  # first time only

with sde.Model(r"e:\RND finale\hello-world\model\sample.mdl") as m:
    m.info()

    r = m.run()
    print("Peak inventory:", r.total_inventory.max())

    r2 = m.run(production_slope=8, production_start_year=2030)
    r2.total_inventory.plot(title="High production scenario")
    plt.show()

    m.derive("annual_growth", lambda res: res.total_inventory.diff().fillna(0))
    r3 = m.run(production_slope=5)
    r3.annual_growth.plot(title="Annual growth rate")
    plt.show()

    df = m.sensitivity("production_slope", [1, 3, 5, 7, 10])
    df.plot(title="Sensitivity: production_slope")
    plt.show()
```

### Example B — model without annotations (active_initial.mdl, no inputs= needed)

```python
import sde
import matplotlib.pyplot as plt

sde.setup()  # first time only

# No inputs= required — all constants are auto-promoted
with sde.Model(
    r"e:\RND finale\SDEverywhere\models\active_initial\active_initial.mdl"
) as m:
    m.info()   # shows 3 Inputs (auto-promoted), 6 Outputs, 0 Constants

    r = m.run()
    r.capacity.plot(title="Capacity over time (default)")
    plt.show()

    r2 = m.run(capacity_adjustment_time=20)
    r2.capacity.plot(title="Slower capacity adjustment")
    plt.show()

    df = m.sensitivity("capacity_adjustment_time", [2, 5, 10, 20, 40])
    df.plot(title="Sensitivity: capacity_adjustment_time")
    plt.show()

    print("Done.")
```

---

## 14. API Reference

### `sde.setup(verbose=True)`
Install SDEverywhere npm packages into `~/.sde_env/`. Safe to call multiple times.

### `sde.is_setup() → bool`
Return `True` if the npm environment has been installed.

### `sde.env_path() → Path`
Return the path to the global npm environment (`~/.sde_env`).

---

### `sde.Model(mdl_path, inputs=None, verbose=True, force_recompile=False, cache_dir=None)`

| Parameter | Type | Description |
|-----------|------|-------------|
| `mdl_path` | str \| Path | Path to the `.mdl` file |
| `inputs` | list[dict \| str] \| None | Explicit input declarations (see section 4) |
| `verbose` | bool | Print progress messages |
| `force_recompile` | bool | Ignore hash cache, always recompile |
| `cache_dir` | str \| Path \| None | Custom directory for compiled artefacts |

**Input dict keys:**

| Key | Required | Description |
|-----|----------|-------------|
| `varName` | ✅ | Exact Vensim variable name |
| `default` or `defaultValue` | ❌ | Default value (falls back to .mdl constant) |
| `min` or `minValue` | ❌ | Min value (default: 0) |
| `max` or `maxValue` | ❌ | Max value (default: 100) |

---

### `Model.run(**kwargs) → ModelResult`
Run the model. Keyword arguments are Python-style input names. Returns `ModelResult`.

### `Model.info()`
Print a formatted summary of all model variables.

### `Model.derive(name: str, fn: callable)`
Register a derived variable computed from `ModelResult`.

### `Model.sensitivity(param, values, **fixed_kwargs) → pd.DataFrame`
Sweep `param` over `values`, return DataFrame of output time-series.

### `Model.load(new_mdl_path)`
Hot-swap the model file. Recompiles if changed.

### `Model.reload()`
Force recompile the current `.mdl` file.

### `Model.close()`
Terminate the Node.js runner process.

---

### `ModelResult`

| Access pattern | Returns |
|----------------|---------|
| `result.variable_name` | `pd.Series` (time-indexed) |
| `result["variable_name"]` | `pd.Series` (same) |
| `result.time` | `pd.Series` of time values |
| `result.at(time_value)` | `dict[str, float]` snapshot at a time |
| `result.keys()` | `list[str]` of all variable names |

---

## 15. Common Errors & Fixes

### `RuntimeError: SDEverywhere environment not found`
Run `sde.setup()` first.

### `BackendUnavailable: Cannot import 'setuptools.backends.legacy'`
Your setuptools is too old. The `pyproject.toml` now uses `setuptools.build_meta` (available since setuptools 42) which avoids this. If you still see it, upgrade setuptools:
```bash
pip install --upgrade setuptools
```

### `FileNotFoundError: [WinError 2]` during setup
Node.js is not on your PATH. Install Node.js from https://nodejs.org and restart your terminal/Jupyter kernel.

### `ValueError: Unknown input: 'my_var'`
The input name is wrong or the variable was not declared in `inputs=[...]`. Call `m.info()` to see valid Python names.

### `AttributeError: No variable 'x' in result`
The variable name `x` does not exist in this model's outputs. Common cause: code written for `sample.mdl` (which has `total_inventory`) being run against a different model like `active_initial.mdl` (which has `capacity`, `production`, etc.). Call `result.keys()` or `m.info()` to see what's available.

### `RuntimeError: sde bundle failed`
The `.mdl` file has a syntax error, an unsupported Vensim function, or an input `varName` that doesn't exist in the model. Check the error message printed above the exception.

### After editing `.mdl`, old results still appear
Call `m.reload()` to force recompilation.

### `ValueError: Unknown input: 'capacity'` (or any output variable name)
You passed an output variable name to `m.run()` or `m.sensitivity()`. Only **inputs** (constants in the `.mdl`) can be overridden. Stocks (`INTEG`), auxiliaries, and any computed variable are outputs — they are calculated by the model and cannot be set externally. Call `m.info()` to see the correct input names.

### `sde bundle failed: … DELAY FIXED / ALLOCATE AVAILABLE / DEPRECIATE STRAIGHTLINE / GAMMA LN function not yet implemented`
The model uses a Vensim function that SDEverywhere's JavaScript code generator does not yet implement. This is an SDE limitation — see [LIMITATIONS.md](LIMITATIONS.md) for the full list.

### `sde bundle failed: … workbook tagged ?name not found`
The model uses a tagged workbook reference (`?tagname`) that requires extra SDE configuration. See [LIMITATIONS.md](LIMITATIONS.md).
