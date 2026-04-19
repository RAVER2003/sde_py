"""
result.py — ModelResult wraps the raw JSON output from the Node runner.

Provides:
  - Attribute / key access to output time-series as pandas Series
  - Point-in-time lookup: result.at(2050)
  - Derived variable registration (via Model.derive)
  - Repr for easy inspection
"""

from __future__ import annotations

from typing import Any, Callable
import pandas as pd


class ModelResult:
    """
    Wraps the output of a single model run.

    Attributes
    ----------
    time : pandas.Series
        The time axis of the simulation.
    meta : dict
        The parsed model metadata (inputs, outputs, time bounds, etc.).

    Variable access
    ---------------
    Access any output variable using its Python name as an attribute or key::

        result.total_inventory          # pandas.Series
        result["total_inventory"]       # same
        result.at(2050)                 # dict of all variable values at t=2050

    Derived variables (registered via Model.derive) are also accessible as
    attributes once the result is created.
    """

    def __init__(
        self,
        raw: dict[str, Any],
        meta: dict[str, Any],
        derived_fns: dict[str, Callable[["ModelResult"], pd.Series]] | None = None,
    ) -> None:
        self._time = pd.Series(raw["time"], name="time")

        # Map from pythonName → pandas Series for every output variable
        self._series: dict[str, pd.Series] = {}
        vensim_to_py = meta.get("vensimToPy", {})

        for var_id, values in raw["outputs"].items():
            # var_id is an SDE-style id like "_total_inventory"
            series = pd.Series(values, index=self._time, name=var_id)
            self._series[var_id] = series

            # Also register under python name if we can look it up
            # SDE var ids start with underscore and use underscores; strip leading _
            stripped = var_id.lstrip("_")
            self._series[stripped] = series

        # Derived variables
        self._derived_fns = derived_fns or {}
        for py_name, fn in self._derived_fns.items():
            self._series[py_name] = fn(self)

        self.meta = meta

    # ── Time series access ───────────────────────────────────────────────────

    @property
    def time(self) -> pd.Series:
        return self._time

    def _resolve(self, name: str) -> pd.Series:
        if name in self._series:
            return self._series[name]
        raise KeyError(
            f"No variable '{name}' in result. "
            f"Available: {list(self._series.keys())}"
        )

    def __getattr__(self, name: str) -> pd.Series:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._resolve(name)
        except KeyError as e:
            raise AttributeError(str(e)) from e

    def __getitem__(self, name: str) -> pd.Series:
        return self._resolve(name)

    def keys(self) -> list[str]:
        """Return all accessible variable names."""
        return list(self._series.keys())

    # ── Point-in-time lookup ─────────────────────────────────────────────────

    def at(self, time_value: float) -> dict[str, float]:
        """
        Return a dict of all output variable values at a specific time point.

        Uses nearest-neighbour lookup so fractional time values work with
        integer time steps.

        Parameters
        ----------
        time_value : float
            The simulation time to query.

        Returns
        -------
        dict mapping python_name → float
        """
        pos = int((self._time - time_value).abs().argmin())
        return {k: float(v.iloc[pos]) for k, v in self._series.items()}

    # ── Repr ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        time = self.meta.get("time", {})
        vars_str = ", ".join(self.keys()[:6])
        if len(self.keys()) > 6:
            vars_str += ", ..."
        return (
            f"<ModelResult "
            f"t={time.get('initial')}→{time.get('final')} "
            f"vars=[{vars_str}]>"
        )
