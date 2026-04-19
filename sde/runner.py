"""
runner.py — Node.js subprocess bridge for SDEverywhere models.

Spawns a persistent `node node_runner.mjs <generated-model.js>` process.
Communicates via newline-delimited JSON over stdin/stdout.
Thread-safe: a lock serialises concurrent run() calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .env import node_runner_path, env_path


class NodeRunner:
    """
    Wraps a long-lived Node.js process that runs the SDE model synchronously.

    Usage::

        runner = NodeRunner.spawn(generated_model_path)
        result = runner.run([1, 2020, 30])
        runner.terminate()
    """

    def __init__(self, proc: subprocess.Popen, metadata: dict[str, Any]) -> None:
        self._proc = proc
        self._lock = threading.Lock()
        self.metadata = metadata  # from the ready message

    # ── Constructor ──────────────────────────────────────────────────────────

    @classmethod
    def spawn(cls, generated_model_path: Path) -> "NodeRunner":
        """
        Start the Node runner subprocess.

        Raises RuntimeError if the process fails to start or does not
        send the expected ready message.
        """
        generated_model_path = Path(generated_model_path).resolve()
        runner_script = node_runner_path()

        ep = env_path()
        node_path_env = str(ep / "node_modules")
        env_vars = {
            **_base_env(),
            "NODE_PATH": node_path_env,
        }

        proc = subprocess.Popen(
            ["node", str(runner_script), str(generated_model_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            env=env_vars,
        )

        # Read the ready line
        ready_line = proc.stdout.readline()
        if not ready_line:
            stderr_out = proc.stderr.read()
            raise RuntimeError(
                f"Node runner failed to start.\nstderr: {stderr_out}"
            )

        try:
            ready_msg = json.loads(ready_line)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Node runner sent invalid JSON on startup: {ready_line!r}\n{e}"
            )

        if "error" in ready_msg:
            raise RuntimeError(f"Node runner error: {ready_msg['error']}")
        if not ready_msg.get("ready"):
            raise RuntimeError(f"Unexpected startup message: {ready_msg}")

        return cls(proc, ready_msg)

    # ── Public API ───────────────────────────────────────────────────────────

    def run(self, inputs: list[float]) -> dict[str, Any]:
        """
        Run the model synchronously with the given input values.

        Parameters
        ----------
        inputs:
            Ordered list of input values, matching the order the model was
            compiled with (same order as meta['inputs']).

        Returns
        -------
        dict with keys:
            "time"    : list[float]
            "outputs" : dict[str, list[float]]  — keyed by SDE var-id (_snake_case)
        """
        cmd = json.dumps({"inputs": [float(v) for v in inputs]}) + "\n"

        with self._lock:
            if self._proc.poll() is not None:
                raise RuntimeError("Node runner process has terminated.")
            self._proc.stdin.write(cmd)
            self._proc.stdin.flush()
            response_line = self._proc.stdout.readline()

        if not response_line:
            stderr_out = self._proc.stderr.read()
            raise RuntimeError(
                f"Node runner closed without responding.\nstderr: {stderr_out}"
            )

        try:
            result = json.loads(response_line)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Node runner returned invalid JSON: {response_line!r}\n{e}"
            )

        if "error" in result:
            raise RuntimeError(f"Model run error: {result['error']}")

        return result

    def terminate(self) -> None:
        """Shut down the Node runner process gracefully."""
        try:
            if self._proc.poll() is None:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()

    def __enter__(self) -> "NodeRunner":
        return self

    def __exit__(self, *_) -> None:
        self.terminate()

    def is_alive(self) -> bool:
        return self._proc.poll() is None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _base_env() -> dict[str, str]:
    """Return a clean environment dict (inheriting PATH so 'node' is found)."""
    import os
    return dict(os.environ)
