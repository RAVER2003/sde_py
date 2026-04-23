"""
model.py — High-level Model class: the primary public API of sde-py.

Workflow::

    import sde
    sde.setup()                          # first-time: installs npm packages

    m = sde.Model('path/to/model.mdl')  # parse + compile + spawn Node runner
    m.info()                             # print variable summary

    result = m.run(production_slope=5)  # run with overridden inputs
    result.total_inventory.plot()

    m.load('other_model.mdl')           # hot-swap the model file
    m.reload()                          # recompile after external changes

    m.derive('revenue', lambda r: r.total_inventory * 10)
    result2 = m.run()
    print(result2.revenue)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .env import require_setup
from .parser import parse_mdl
from .compiler import compile_mdl
from .runner import NodeRunner
from .result import ModelResult


class Model:
    """
    Represents a running SDEverywhere model instance.

    Parameters
    ----------
    mdl_path : str | Path
        Path to the Vensim .mdl file.
    verbose : bool
        Print progress messages (default: True).
    force_recompile : bool
        Force recompilation even if .mdl hasn't changed (default: False).
    cache_dir : str | Path | None
        Directory where compiled artefacts are stored.
        Defaults to a persistent subdirectory inside ~/.sde_env/model-cache/.
    inputs : list[dict | str] | None
        Explicit input variable declarations.  Each entry is either:

        - A **dict** with at minimum ``varName`` (the Vensim variable name) and
          optionally ``default``, ``min``, ``max``:

          .. code-block:: python

              {"varName": "Capacity Adjustment Time", "default": 10, "min": 1, "max": 50}

        - A **string** (just the Vensim variable name); min/max default to 0/100 and
          the default value is taken from the model constant's value if available.

        These are merged with any inputs already defined by ``[min,max]`` annotations
        in the ``.mdl`` file.  Explicit declarations take precedence.
    """

    def __init__(
        self,
        mdl_path: str | Path,
        inputs: "list[dict | str] | None" = None,
        verbose: bool = True,
        force_recompile: bool = False,
        cache_dir: str | Path | None = None,
    ) -> None:
        self._env_path = require_setup()
        self._verbose = verbose
        self._derived_fns: dict[str, Callable] = {}
        self._extra_inputs: list[dict | str] = inputs or []

        self._load_and_compile(
            Path(mdl_path),
            force=force_recompile,
            cache_dir=Path(cache_dir) if cache_dir else None,
        )

    # ── Initialisation helpers ───────────────────────────────────────────────

    def _load_and_compile(
        self,
        mdl_path: Path,
        force: bool = False,
        cache_dir: Path | None = None,
    ) -> None:
        self._mdl_path = mdl_path.resolve()

        if cache_dir is None:
            cache_dir = (
                self._env_path
                / "model-cache"
                / _safe_name(str(self._mdl_path))
            )

        self._cache_dir = cache_dir

        if self._verbose:
            print(f"→ Parsing {self._mdl_path.name} ...")
        self._meta = parse_mdl(self._mdl_path)

        # Merge any user-declared inputs into the parsed metadata
        _merge_inputs(self._meta, self._extra_inputs)

        # Auto-promote any remaining constants that weren't explicitly declared
        _auto_promote_constants(self._meta)

        self._gen_model = compile_mdl(
            mdl_path=self._mdl_path,
            meta=self._meta,
            cache_dir=self._cache_dir,
            verbose=self._verbose,
            force=force,
        )

        if self._verbose:
            print("→ Starting Node runner ...")
        if hasattr(self, "_runner") and self._runner is not None:
            try:
                self._runner.terminate()
            except Exception:
                pass

        self._runner = NodeRunner.spawn(self._gen_model)

        # Merge runner metadata (startTime/endTime) into meta["time"]
        rm = self._runner.metadata
        self._meta["time"].update({
            "initial": rm.get("startTime", self._meta["time"].get("initial")),
            "final":   rm.get("endTime",   self._meta["time"].get("final")),
            "saveper": rm.get("saveFreq",   self._meta["time"].get("saveper")),
        })
        self._output_var_ids: list[str] = rm.get("outputVarIds", [])

        if self._verbose:
            print("✓ Model ready.\n")

    # ── Public API ───────────────────────────────────────────────────────────

    def run(self, **kwargs: float) -> ModelResult:
        """
        Run the model, optionally overriding one or more inputs.

        Keyword arguments are Python-style variable names (snake_case).

        Returns a :class:`ModelResult`.

        Example::

            result = model.run(production_slope=5, production_start_year=2030)
        """
        inputs = self._build_inputs(**kwargs)
        raw = self._runner.run(inputs)
        return ModelResult(raw, self._meta, self._derived_fns)

    def info(self) -> None:
        """Print a human-readable summary of the model variables."""
        m = self._meta
        print(f"Model: {self._mdl_path.name}")
        print(
            f"  Time: {m['time']['initial']} → {m['time']['final']} "
            f"(step={m['time']['step']})"
        )
        print()
        print("  Inputs:")
        for v in m["inputs"]:
            print(
                f"    {v['pythonName']!r:35s}  "
                f"[{v['minValue']}, {v['maxValue']}]  "
                f"default={v['defaultValue']}"
            )
        print()
        print("  Outputs:")
        for v in m["outputs"]:
            # equation is already a clean single-line string (parser applies reduceWhitespace)
            eq = v["equation"]
            if len(eq) > 72:
                eq = eq[:69] + "..."
            print(f"    {v['pythonName']!r:35s}  {eq}")
        if m["constants"]:
            print()
            print("  Constants:")
            for v in m["constants"]:
                print(f"    {v['pythonName']!r:35s}  = {v['value']}")
        if self._derived_fns:
            print()
            print("  Derived:")
            for name in self._derived_fns:
                print(f"    {name!r}")

    def derive(self, name: str, fn: Callable[["ModelResult"], pd.Series]) -> None:
        """
        Register a derived variable.

        Parameters
        ----------
        name : str
            Python attribute name to access the derived series.
        fn : callable
            Called with a :class:`ModelResult` and must return a ``pd.Series``.

        Example::

            model.derive('revenue', lambda r: r.total_inventory * 42)
        """
        self._derived_fns[name] = fn

    def sensitivity(
        self,
        param: str,
        values: list[float],
        **fixed_kwargs: float,
    ) -> pd.DataFrame:
        """
        Run the model for each value of ``param`` and return a DataFrame of results.

        Parameters
        ----------
        param : str
            Python name of the input variable to sweep.
        values : list[float]
            Values to test.
        **fixed_kwargs :
            Other input overrides applied to all runs.

        Returns
        -------
        pd.DataFrame
            Columns are the output variable time-series for each value of ``param``.
            Index is time.  Column names are ``f"{param}={v}"``.

        Example::

            df = model.sensitivity('production_slope', [1, 3, 5, 7, 10])
            df.plot()
        """
        frames = {}
        for v in values:
            kwargs = {**fixed_kwargs, param: v}
            result = self.run(**kwargs)
            # Use first output variable for the DataFrame column
            first_key = next(
                k for k in result.keys()
                if not k.startswith("_") and k != "time"
            )
            frames[f"{param}={v}"] = result[first_key]

        return pd.DataFrame(frames)

    def load(self, new_mdl_path: str | Path) -> None:
        """
        Hot-swap the model file.  Recompiles if the new file has changed.
        Restarts the Node runner automatically.
        """
        self._load_and_compile(Path(new_mdl_path), force=False)

    def reload(self) -> None:
        """Force recompilation of the current .mdl file and restart the runner."""
        self._load_and_compile(self._mdl_path, force=True)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Shut down the Node runner process."""
        if hasattr(self, "_runner") and self._runner is not None:
            self._runner.terminate()
            self._runner = None

    def __enter__(self) -> "Model":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ── Private helpers ──────────────────────────────────────────────────────

    def _build_inputs(self, **kwargs: float) -> list[float]:
        """
        Build the ordered input list for the runner.

        Starts from default values, then applies any overrides given as kwargs.
        """
        py_to_vensim = self._meta.get("pyToVensim", {})
        meta_inputs = self._meta["inputs"]

        defaults = {v["pythonName"]: v["defaultValue"] for v in meta_inputs}

        for key, val in kwargs.items():
            if key not in defaults:
                available = list(defaults.keys())
                raise ValueError(
                    f"Unknown input: '{key}'.  Available: {available}"
                )
            defaults[key] = val

        # Return values in the same order as the parsed inputs list
        return [defaults[v["pythonName"]] for v in meta_inputs]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_name(path_str: str) -> str:
    """Convert an absolute path to a safe directory name for cache storage."""
    import re
    safe = re.sub(r"[^\w]", "_", path_str)
    return safe[:80]


def _merge_inputs(meta: dict, extra: "list[dict | str]") -> None:
    """Merge user-declared inputs into meta["inputs"], in-place.

    Each entry in ``extra`` is either:
      - A string  → just the Vensim variable name; min/max/default filled from
                    the constants table if available, otherwise 0/100/0.
      - A dict    → must have ``varName``; optionally ``default``, ``min``, ``max``.

    Variables already present in meta["inputs"] (from [min,max] annotations) are
    left unchanged unless explicitly overridden.  User declarations take precedence.
    Variables found in meta["constants"] are removed from there and added to inputs.
    """
    if not extra:
        return

    from .parser import to_python_name

    # Build a lookup from pythonName → constant value for fast access
    const_by_py: dict[str, float] = {
        c["pythonName"]: c["value"] for c in meta["constants"]
    }
    const_by_vensim: dict[str, dict] = {
        c["varName"].lower(): c for c in meta["constants"]
    }

    # Existing input python names (to avoid duplicates)
    existing_py: set[str] = {v["pythonName"] for v in meta["inputs"]}

    new_inputs: list[dict] = []
    to_remove_from_constants: set[str] = set()

    for entry in extra:
        # Normalise to dict
        if isinstance(entry, str):
            spec: dict = {"varName": entry}
        else:
            spec = dict(entry)

        var_name: str = spec["varName"]
        py_name = to_python_name(var_name)

        # Skip if already in inputs (mdl [min,max] annotation already handled it)
        if py_name in existing_py:
            continue

        # Look up default value from constants table
        const_entry = const_by_vensim.get(var_name.lower().strip())
        mdl_default = const_entry["value"] if const_entry else 0.0

        default_val = float(spec.get("default", spec.get("defaultValue", mdl_default)))
        min_val     = float(spec.get("min",     spec.get("minValue",     0.0)))
        max_val     = float(spec.get("max",     spec.get("maxValue",     100.0)))

        new_inputs.append({
            "varName":      var_name,
            "pythonName":   py_name,
            "defaultValue": default_val,
            "minValue":     min_val,
            "maxValue":     max_val,
            "stepValue":    None,
            "doc":          const_entry["doc"] if const_entry else "",
        })
        existing_py.add(py_name)

        # Remove from constants so it doesn't appear in both lists
        if const_entry:
            to_remove_from_constants.add(py_name)

        # Update name-mapping dicts
        meta["pyToVensim"][py_name] = var_name
        meta["vensimToPy"][var_name] = py_name

    # Append new inputs and remove promoted constants
    meta["inputs"].extend(new_inputs)
    meta["constants"] = [
        c for c in meta["constants"]
        if c["pythonName"] not in to_remove_from_constants
    ]


def _auto_promote_constants(meta: dict) -> None:
    """Promote all remaining constants to inputs automatically.

    Called after _merge_inputs so that constants not explicitly declared still
    become controllable inputs.  Uses min=0, max=100 as safe defaults; the
    default value is taken from the constant's .mdl value.

    This means you never need to know a model's constants upfront — every
    constant is reachable via m.run(constant_name=value).
    """
    existing_py: set[str] = {v["pythonName"] for v in meta["inputs"]}
    promoted: list[dict] = []

    for c in meta["constants"]:
        if c["pythonName"] in existing_py:
            continue
        promoted.append({
            "varName":      c["varName"],
            "pythonName":   c["pythonName"],
            "defaultValue": c["value"],
            "minValue":     0.0,
            "maxValue":     100.0,
            "stepValue":    None,
            "doc":          c.get("doc", ""),
        })
        existing_py.add(c["pythonName"])
        meta["pyToVensim"][c["pythonName"]] = c["varName"]
        meta["vensimToPy"][c["varName"]] = c["pythonName"]

    meta["inputs"].extend(promoted)
    meta["constants"] = []  # all promoted


