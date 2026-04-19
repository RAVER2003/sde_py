"""
compiler.py — Compile a .mdl file to generated-model.js using SDEverywhere.

Steps:
  1. MD5-hash the .mdl file; skip if hash matches cached value.
  2. Auto-generate sde.config.js from parsed inputs/outputs.
  3. Run `sde bundle` via the global npm env.
  4. Copy generated-model.js into the cache dir.
  5. Write the new hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .env import sde_cli_path, env_path


# ── Config generation ───────────────────────────────────────────────────────

def _generate_sde_config(
    mdl_path: Path,
    inputs: list[dict],
    outputs: list[dict],
    cache_dir: Path,
) -> Path:
    """Write an sde.config.js into cache_dir and return its path."""

    # Build relative path from cache_dir to mdl_path
    try:
        rel_mdl = mdl_path.resolve().relative_to(cache_dir.resolve())
        mdl_rel_str = rel_mdl.as_posix()
    except ValueError:
        # If not relative, use absolute
        mdl_rel_str = mdl_path.resolve().as_posix()

    inputs_js = json.dumps(
        [
            {
                "varName": v["varName"],
                "defaultValue": v["defaultValue"] if v["defaultValue"] is not None else 0,
                "minValue": v["minValue"] if v["minValue"] is not None else 0,
                "maxValue": v["maxValue"] if v["maxValue"] is not None else 100,
            }
            for v in inputs
        ],
        indent=8,
    )

    outputs_js = json.dumps(
        [
            {"varName": v["varName"]}
            for v in outputs
            # Exclude subscripted outputs whose subscripts use specific elements
            # (not dimension names) — SDE cannot resolve them by bare name in spec.json.
            # Non-subscripted outputs and fully-dimensioned subscripted outputs are fine.
            if not v.get("isSubscripted", False) or v.get("allDimSubscripts", False)
        ],
        indent=8,
    )

    config_content = f"""import {{ workerPlugin }} from '@sdeverywhere/plugin-worker'

export async function config() {{
  return {{
    modelFiles: ['{mdl_rel_str}'],

    modelSpec: async () => {{
      return {{
        inputs: {inputs_js},
        outputs: {outputs_js},
      }}
    }},

    plugins: [
      workerPlugin(),
    ],
  }}
}}
"""

    config_path = cache_dir / "sde.config.js"
    config_path.write_text(config_content, encoding="utf-8")
    return config_path


# ── Hash helpers ────────────────────────────────────────────────────────────

def _md5(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def _cached_hash(cache_dir: Path) -> str | None:
    hash_file = cache_dir / "model-hash.txt"
    if hash_file.exists():
        return hash_file.read_text().strip()
    return None


def _write_hash(cache_dir: Path, digest: str) -> None:
    (cache_dir / "model-hash.txt").write_text(digest)


# ── Package.json for cache dir ──────────────────────────────────────────────

def _write_package_json(cache_dir: Path) -> None:
    """Write a package.json so that Node can resolve @sdeverywhere/* from the global env."""
    ep = env_path()
    pkg = {
        "name": "sde-cache",
        "version": "1.0.0",
        "private": True,
        "type": "module",
    }
    (cache_dir / "package.json").write_text(json.dumps(pkg, indent=2))


# ── Main compile function ───────────────────────────────────────────────────

def compile_mdl(
    mdl_path: Path,
    meta: dict[str, Any],
    cache_dir: Path,
    verbose: bool = True,
    force: bool = False,
) -> Path:
    """
    Compile mdl_path → generated-model.js in cache_dir.

    Returns the path to generated-model.js.
    Skips compilation if the .mdl file has not changed (hash match).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    mdl_path = mdl_path.resolve()

    current_hash = _md5(mdl_path)
    cached = _cached_hash(cache_dir)
    gen_model = cache_dir / "generated-model.js"

    if not force and cached == current_hash and gen_model.exists():
        if verbose:
            print(f"✓ Model unchanged — skipping recompile ({mdl_path.name})")
        return gen_model

    if verbose:
        print(f"Compiling {mdl_path.name} ...")

    # Run compilation inside ~/.sde_env/compile-workspace/ so that Node's
    # standard upward module resolution finds ~/.sde_env/node_modules/.
    # (Node ESM ignores NODE_PATH, so we can't use an arbitrary cwd.)
    ep = env_path()
    compile_ws = ep / "compile-workspace"
    compile_ws.mkdir(parents=True, exist_ok=True)

    # Write support files into the compile workspace
    _write_package_json(compile_ws)
    _generate_sde_config(
        mdl_path=mdl_path,
        inputs=meta["inputs"],
        outputs=meta["outputs"],
        cache_dir=compile_ws,
    )

    # Locate the sde CLI
    cli = sde_cli_path()

    # Always capture output so we can include it in error messages
    result = subprocess.run(
        f'node "{cli}" bundle --config sde.config.js',
        cwd=compile_ws,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        shell=True,
    )

    if verbose and result.stdout:
        print(result.stdout)
    if verbose and result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        err = (result.stdout or "") + (result.stderr or "")
        # Extract the first human-readable Error: line from the SDE output so the
        # message is concise even when the full JS stack trace is present.
        first_error = next(
            (line.strip() for line in err.splitlines() if line.strip().startswith("Error:")),
            None,
        )
        summary = first_error or err.strip()
        raise RuntimeError(f"sde bundle failed: {summary}")

    # Copy generated-model.js from compile workspace to cache_dir
    sde_prep = compile_ws / "sde-prep" / "generated-model.js"
    if not sde_prep.exists():
        raise FileNotFoundError(
            f"sde bundle succeeded but generated-model.js not found at {sde_prep}"
        )
    shutil.copy2(sde_prep, gen_model)

    _write_hash(cache_dir, current_hash)

    if verbose:
        print(f"✓ Compiled → {gen_model}")

    return gen_model
