# sde-py

A Python library for running [Vensim](https://vensim.com/) `.mdl` system-dynamics models via [SDEverywhere](https://sdeverywhere.org/). No Vensim licence required — models compile to JavaScript and run through a persistent Node.js process, with results returned as `pandas` DataFrames.

---

## Requirements

- **Python 3.9+**
- **Node.js 18+** — install from https://nodejs.org
- **pip** (comes with Python)

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/RAVER2003/sde_py.git
cd sde_py

# 2. Install the Python package (editable mode so changes take effect immediately)
pip install -e .

# 3. Download the SDEverywhere npm packages (one-time, ~30 seconds)
sde-setup
```

> `sde-setup` installs the npm packages into `~/.sde_env/`. You only need to run it once. It is safe to run again — it will skip if already installed.

---

## Quick Start

```python
import sde

# Load a model — compiles on first load (~10 s), instant on subsequent loads
m = sde.Model(r"path/to/your_model.mdl")

# Run with default parameters
result = m.run()
print(result.keys())          # list all output variable names
print(result.total_inventory) # pandas Series, time-indexed

# Override any constant
result2 = m.run(production_slope=8, production_start_year=2030)

# Sensitivity sweep
df = m.sensitivity("production_slope", [1, 3, 5, 7, 10])
df.plot(title="Sensitivity: production_slope")

m.close()
```

### Context manager (recommended)

```python
with sde.Model(r"path/to/model.mdl") as m:
    r = m.run()
    print(r.total_inventory.max())
# Node.js process shut down automatically
```

---

## Key Features

| Feature | Details |
|---|---|
| **Zero Vensim licence** | Models compile once via SDE CLI, run in pure JS |
| **Fast repeated runs** | Persistent Node.js process — each run ~1 ms overhead |
| **Auto-promotes constants** | Every constant becomes a sweepable input automatically |
| **Pandas integration** | All outputs are `pd.Series` indexed by simulation time |
| **Sensitivity analysis** | `m.sensitivity(param, values)` returns a `pd.DataFrame` |
| **Derived variables** | `m.derive("name", lambda r: ...)` computed after every run |
| **Hot-swap** | `m.load(new_path)` or `m.reload()` without restarting Python |
| **MD5 cache** | Recompiles only when the `.mdl` file actually changes |

---

## Documentation

| File | Contents |
|---|---|
| [TUTORIAL.md](TUTORIAL.md) | Full walkthrough — installation through API reference |
| [INTERNALS.md](INTERNALS.md) | Architecture, data flow, design decisions |
| [LIMITATIONS.md](LIMITATIONS.md) | Variable types, what can/cannot be inputs, SDE engine limits |

---

## API Summary

```python
sde.setup()                    # install npm packages into ~/.sde_env/
sde.is_setup()                 # True if already installed

m = sde.Model(
    "model.mdl",
    inputs=None,               # explicit input declarations (optional)
    verbose=True,              # print progress
    force_recompile=False,     # ignore hash cache
    cache_dir=None,            # custom cache directory
)

m.info()                       # print all inputs and outputs
m.run(**kwargs)                # → ModelResult
m.sensitivity(param, values, **fixed)  # → pd.DataFrame
m.derive(name, fn)             # register derived variable
m.load(new_path)               # hot-swap model file
m.reload()                     # force recompile current file
m.close()                      # shut down Node.js runner

result.variable_name           # pd.Series (time-indexed)
result["variable_name"]        # same
result.time                    # pd.Series of time values
result.at(t)                   # dict snapshot at time t
result.keys()                  # list of all variable names
```

---

## Supported Variable Types

| Vensim type | Input? | Output? |
|---|---|---|
| Constant | ✅ auto-promoted | — |
| Auxiliary | — | ✅ |
| Stock (INTEG) | — | ✅ |
| Lookup table | — | — (baked in at compile) |
| Subscripted (root dim) | — | ✅ |
| TABBED ARRAY / GET DIRECT LOOKUPS | — | — (excluded) |

See [LIMITATIONS.md](LIMITATIONS.md) for full details and edge cases.

---

## License

MIT
