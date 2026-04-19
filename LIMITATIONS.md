# sde-py Limitations and Edge Cases

This document covers:
- What Vensim variable types exist and how sde-py handles each
- What can and cannot be used as inputs or outputs
- Edge cases per variable type
- Known hard limitations of the underlying SDEverywhere engine
- Sensitivity analysis scope

---

## 1. Vensim Variable Types — A Primer

Vensim models are built from six fundamental variable types. Understanding them is essential to understanding what sde-py can and cannot do.

### 1.1 Constants

```vensim
Population = 1000
    ~ people
    ~|
```

A constant is a variable whose value is a plain number. It does not change during a simulation run unless you override it via `m.run(population=2000)`. Constants are the only variables that sde-py can use as **inputs**.

**sde-py behaviour:** Every constant is auto-promoted to an input. You can override any constant by name in `m.run()`.

**Edge cases:**
- A constant with `[min,max]` annotation in units (e.g. `~ people [0,10000,100]`) is explicitly flagged as an input slider; sde-py reads the range and uses it.
- A constant set to `0` or a negative number is valid — `m.run(my_const=-5)` works.
- `INITIAL TIME`, `FINAL TIME`, `TIME STEP`, and `SAVEPER` are special constants. sde-py extracts them into `meta["time"]`. Changing them at runtime requires `force_recompile=True` because the compiled model's time loop is fixed at compile time.

---

### 1.2 Auxiliaries (Computed Variables)

```vensim
Production Rate = Capacity * Utilization
    ~ units/year
    ~|
```

An auxiliary is recalculated every timestep from other variables. It is an **output** — you observe it, not set it.

**sde-py behaviour:** Auxiliaries appear in `m.info()` under *Outputs*. You can read them from `ModelResult` via `result.production_rate`.

**Edge cases:**
- You **cannot** pass an auxiliary name to `m.run()` or `m.sensitivity()`. It will raise `ValueError: Unknown input: 'production_rate'`. To influence an auxiliary, sweep the constant inputs that feed it.
- If an auxiliary references an external lookup table (`GET DIRECT DATA`, `GET XLS DATA`), it will only work if those data files exist relative to the `.mdl` file.

---

### 1.3 Stocks (INTEG variables)

```vensim
Capacity = INTEG(Capacity Adjustment Rate, Initial Target Capacity)
    ~ units
    ~|
```

A stock integrates (accumulates) a flow over time. The first argument is the **rate of change** (flow); the second is the **initial value** (which can be a constant or another variable).

**sde-py behaviour:** Stocks are **outputs**. They appear under *Outputs* in `m.info()`.

**Edge cases:**
- The stock's initial value is often a constant (e.g. `Initial Target Capacity = 100`). You *can* sweep that constant to change where the stock starts:
  ```python
  df = m.sensitivity("initial_target_capacity", [50, 100, 200, 400])
  ```
- You **cannot** pass the stock variable itself as a sensitivity parameter:
  ```python
  # WRONG — capacity is an output, not an input
  m.sensitivity("capacity", [50, 100, 200])  # raises ValueError
  
  # CORRECT — sweep an input that drives capacity
  m.sensitivity("capacity_adjustment_time", [2, 5, 10, 20, 40])
  ```
- The error message will say `ValueError: Unknown input: 'capacity'. Available: [...]`. This does *not* mean capacity is missing — it means you tried to use an output as an input.
- Stocks accumulate state across the simulation. A stock cannot be "reset mid-run"; only the full time series result is available.

---

### 1.4 Lookups

```vensim
Utilization Effect(
    [(0,0)-(2,2)],
    (0,0),(0.25,0.376),(0.5,0.691),(1,1),(1.25,1.130),(2,1.5))
    ~ dimensionless
    ~|
```

A lookup is a piecewise-linear function table. It is called with a value and returns an interpolated result.

**sde-py behaviour:** Lookups have no `=` equation — the parser skips them entirely (they are neither inputs nor outputs). SDE embeds them directly in the compiled JavaScript.

**Edge cases:**
- You cannot inspect or modify lookup values through sde-py. They are baked in at compile time.
- `GET DIRECT LOOKUPS(...)` and `GET XLS LOOKUPS(...)` are lookup tables loaded from external files. They are silently excluded from `spec.json` outputs because SDE handles them internally.

---

### 1.5 Subscripted Variables

```vensim
Production[Region] = Base Rate[Region] * Capacity[Region]
    ~ units/year
    ~|

Base Rate[Region] = 1, 2, 3
    ~ units/year
    ~|
```

A subscripted variable is an array — one value per element of a dimension (e.g. `Region: East, West, North`).

**sde-py behaviour:**
- Variables subscripted by a **root dimension** only (e.g. `var[DimA]`) are included in outputs.
- Variables subscripted by a **sub-range** (e.g. `var[SubA]` where `SubA ⊂ DimA`) are excluded — SDE cannot resolve sub-range subscripts in `spec.json`.
- Variables using `:EXCEPT:` clauses are excluded — they define only part of the array.
- **Numeric array constants** (`Base Rate[Region] = 1, 2, 3`) are excluded from outputs — SDE initialises them internally.
- **Hierarchical dimension variables** (subscript elements are themselves dimension names) are excluded.

**Edge cases:**
- If you define `a[DimA] = 10, 20, 30` (numeric array), it will not appear in outputs. This is intentional — SDE stores the array internally and uses it in the compiled model.
- `TABBED ARRAY(...)` syntax is treated the same as a numeric array constant — excluded from outputs.

---

### 1.6 Dimension Definitions

```vensim
Region: East, West, North
    ~|
```

A dimension definition names the subscripts in an array dimension. It is not a model variable — it is structural metadata.

**sde-py behaviour:** Dimension names are collected but not exposed as variables. They are used only to classify subscripted variables.

---

## 2. Inputs vs Outputs — Summary Table

| Variable type | Can be `m.run(...)` input? | Appears as output? | Notes |
|---|---|---|---|
| **Constant** | ✅ Yes | ❌ No (moved to inputs) | Auto-promoted |
| **Auxiliary** | ❌ No | ✅ Yes | Recalculated every step |
| **Stock (INTEG)** | ❌ No | ✅ Yes | Accumulated state |
| **Stock initial value constant** | ✅ Yes | ❌ No | Promotes like any constant |
| **Lookup table** | ❌ No | ❌ No | Baked in at compile time |
| **Numeric array constant** | ❌ No | ❌ No | SDE initialises internally |
| **TABBED ARRAY** | ❌ No | ❌ No | Inline data table |
| **GET DIRECT/XLS LOOKUPS** | ❌ No | ❌ No | External lookup table |
| **Subscripted (root dim)** | ❌ No | ✅ Yes | Whole array as output |
| **Subscripted (sub-range)** | ❌ No | ❌ No | SDE cannot resolve sub-range |
| **:EXCEPT: var** | ❌ No | ❌ No | Partial array definition |
| **Dimension definition** | ❌ No | ❌ No | Structural only |
| **INITIAL/FINAL TIME** | ⚠️ Requires recompile | ❌ No | Fixed at compile time |

---

## 3. Sensitivity Analysis Limitations

`m.sensitivity(param, values, **fixed_kwargs)` sweeps **one input parameter** across multiple values. Key constraints:

1. **`param` must be an input** (a constant promoted to input). Passing an auxiliary or stock name raises:
   ```
   ValueError: Unknown input: 'capacity'. Available: ['capacity_adjustment_time', ...]
   ```

2. **One output variable is returned** — the first output variable in the compiled model order. For multi-output models, use `m.run()` in a loop and build your own DataFrame.

3. **`fixed_kwargs` must also be inputs.** Passing an output name there raises the same error.

### Pattern for sweeping an input while observing a specific output

```python
results = {}
for v in [2, 5, 10, 20, 40]:
    r = m.run(capacity_adjustment_time=v)
    results[f"adj_time={v}"] = r.capacity   # observe the stock output

import pandas as pd
df = pd.DataFrame(results)
df.plot(title="Capacity vs Adjustment Time")
```

---

## 4. Time Control Variables

`INITIAL TIME`, `FINAL TIME`, `TIME STEP`, and `SAVEPER` are compiled into the model's JavaScript loop. Changing them with `m.run(final_time=200)` **does not work** — the compiled model ignores those overrides.

To change the simulation horizon:
1. Edit the `.mdl` file directly (change `FINAL TIME = 100`)
2. Reload with `m.reload()` (forces recompilation)

```python
# Correct workflow for changing FINAL TIME:
# 1. Edit the .mdl file to set FINAL TIME = 200
m.reload()          # force recompile
r = m.run()         # now runs to t=200
```

---

## 5. Known SDEverywhere Engine Limitations

These are hard limits of the underlying SDE JavaScript code generator. They cannot be fixed in sde-py without changes to SDEverywhere itself.

### 5.1 Unimplemented Vensim Functions

The following Vensim built-in functions are not yet implemented in SDE's JavaScript code generator. Models that use them will fail with `sde bundle failed`.

| Vensim function | Affected model(s) | Status |
|---|---|---|
| `ALLOCATE AVAILABLE` / `ALLOCATE BY PRIORITY` | `allocate` | Not in SDE codegen |
| `DELAY FIXED` | `delayfixed`, `delayfixed2` | Not in SDE codegen |
| `DEPRECIATE STRAIGHTLINE` | `depreciate` | Not in SDE codegen |
| `GAMMA LN` | `gamma_ln` | Not in SDE codegen |

**Workaround:** None within sde-py. These require upstream SDE implementation.

---

### 5.2 Tagged Workbook References (`?tagname`)

Some models reference external data files via a symbolic tag rather than a literal filename:

```vensim
a[DimA] = GET DIRECT LOOKUPS('?lookups', 'tab', '1', 'B')
    ~|
```

The `?lookups` token is a *tag name* that must be mapped to an actual file in `sde.config.js`. sde-py auto-generates `sde.config.js` but does not have access to the tag→file mapping without parsing additional Vensim workspace files.

**Affected models:** `directconst`, `directlookups` (in the SDEverywhere test suite)

**Error:** `sde bundle failed: workbook tagged 'lookups' was not found`

**Workaround:** Manually write `sde.config.js` and supply the `directData` mapping. This is outside the current sde-py scope.

---

### 5.3 Binary External Data Files

Vensim can read data from its own binary formats (`.vdfx`, `.dat`). SDE only supports CSV and Excel (`.xlsx`).

**Affected models:** `prune` (uses `.vdfx` and `.dat` files)

**Error:** SDE either cannot parse the file or silently produces incorrect results.

**Workaround:** Export the binary data from Vensim as CSV and update the model to use `GET DIRECT DATA` pointing to the CSV.

---

### 5.4 Missing External Data Files

Some models reference external CSV/Excel files that must be present alongside the `.mdl` file. If those files are missing, `sde bundle` or the model run itself will fail.

**Affected models:** `extdata`, `directdata` (some variants missing source files in certain distributions)

**Error:** `sde bundle failed: cannot find file 'some_data.csv'` or silent wrong results.

**Workaround:** Ensure all referenced external data files are present in the model's directory.

---

## 6. Caching and Recompilation

sde-py caches the compiled `generated-model.js` and only recompiles when the `.mdl` file's MD5 hash changes.

| Action | Triggers recompile? |
|---|---|
| `m.run(some_constant=value)` | ❌ No — just changes an input value |
| Edit the `.mdl` file, then call `m.run()` again | ✅ Yes — hash changes |
| Edit the `.mdl` file and call the same `sde.Model(...)` in the same session | ⚠️ No — must call `m.reload()` or create a new `Model` |
| Change `FINAL TIME` in the `.mdl` | ✅ Yes, after `m.reload()` |

If you notice stale results (model behaves as if your `.mdl` edits didn't apply), call:

```python
m.reload()   # forces recompilation ignoring the hash cache
```

---

## 7. Pass Rate Across SDEverywhere Test Models

As of the current sde-py version, 54 out of 57 SDEverywhere built-in test models pass end-to-end (parse → compile → run → output). The 3 failures are all hard SDE engine limitations:

| Failing model | Reason | Category |
|---|---|---|
| `directconst` | Tagged workbook refs (`?directconst`) | §5.2 |
| `directlookups` | Tagged workbook refs (`?lookups`) + GET DIRECT LOOKUPS | §5.2 |
| `prune` | Binary `.vdfx`/`.dat` data files | §5.3 |

The other previously failing models were fixed by parser improvements in sde-py:

| Fix | Models affected |
|---|---|
| Quote-aware pipe splitting (`_split_on_pipe`) | `preprocess` (input + expected) |
| TABBED ARRAY exclusion from outputs | `preprocess` |
| GET DIRECT LOOKUPS / GET XLS LOOKUPS exclusion | `directlookups` (partial fix) |
| Sub-range dimension detection | Various subscripted models |
| Hierarchical dimension detection | Various subscripted models |
| `:EXCEPT:` clause exclusion | `except`, `except2` and similar |
| Numeric array constant fix (require comma/semicolon) | Various |
