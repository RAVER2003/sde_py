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
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .env import sde_cli_path, env_path


# ── Unsupported-function patching ───────────────────────────────────────────

# Maps SDE-unsupported Vensim function names →
#   (replacement_name, n_args_to_keep, note)
# n_args_to_keep: how many of the original's arguments to forward (rest dropped)
_UNSUPPORTED_SUBSTITUTIONS: dict[str, tuple[str, int, str]] = {
    "DELAY FIXED": (
        "DELAY3",
        2,
        "DELAY3 is a 3rd-order exponential approximation; initial-value arg dropped",
    ),
}

# Match any unsupported function name (case-insensitive, flexible whitespace)
_PATCH_RE = re.compile(
    r"(?i)(" + "|".join(re.escape(k) for k in _UNSUPPORTED_SUBSTITUTIONS) + r")\s*(?=\()"
)


def _extract_n_args(text: str, start: int, n: int) -> tuple[str, int]:
    """
    Starting at `start` (the opening '(' in `text`), extract the first `n`
    comma-separated arguments respecting parenthesis/bracket nesting.
    Returns (joined_args_string, index_after_closing_paren).
    """
    assert text[start] == "("
    args: list[str] = []
    depth = 0
    current: list[str] = []
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "([":
            depth += 1
            if depth > 1:           # depth==1 is the outer call paren — don't include it
                current.append(ch)
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                # closing paren of the function call
                args.append("".join(current).strip())
                return ", ".join(args[:n]), i + 1
            current.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(current).strip())
            current = []
            if len(args) == n:
                # Skip remaining args — find matching close paren
                depth_inner = 1
                i += 1
                while i < len(text) and depth_inner > 0:
                    if text[i] in "([":
                        depth_inner += 1
                    elif text[i] in ")]":
                        depth_inner -= 1
                    i += 1
                return ", ".join(args[:n]), i
        else:
            current.append(ch)
        i += 1
    # fallback: return what we have
    return ", ".join(args[:n]), i


def _patch_unsupported_functions(mdl_text: str, verbose: bool) -> tuple[str, list[str]]:
    """
    Replace Vensim functions that SDE's JS code-gen does not implement with
    supported approximations.  Returns (patched_text, list_of_warnings).
    """
    warnings: list[str] = []
    result: list[str] = []
    pos = 0

    for m in _PATCH_RE.finditer(mdl_text):
        raw_name = re.sub(r"\s+", " ", m.group(1).upper().strip())
        replacement, n_keep, note = _UNSUPPORTED_SUBSTITUTIONS[raw_name]

        # Copy text before this match
        result.append(mdl_text[pos:m.start()])

        # Find opening paren (there may be whitespace between name and '(')
        paren_pos = mdl_text.index("(", m.end())
        kept_args, after_paren = _extract_n_args(mdl_text, paren_pos, n_keep)

        result.append(f"{replacement}({kept_args})")
        pos = after_paren

        warnings.append(f"'{raw_name}' → '{replacement}({kept_args})' ({note})")

    result.append(mdl_text[pos:])
    return "".join(result), warnings


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

    # ── Pre-process: patch unsupported Vensim functions ──────────────────────
    mdl_text = mdl_path.read_text(encoding="utf-8", errors="replace")
    patched_text, patch_warnings = _patch_unsupported_functions(mdl_text, verbose)
    if patch_warnings:
        seen_fns: set[str] = set()
        for w in patch_warnings:
            fn = w.split("'")[1]  # e.g. 'DELAY FIXED'
            replacement = _UNSUPPORTED_SUBSTITUTIONS[fn][0]
            if fn not in seen_fns:
                seen_fns.add(fn)
                print(
                    f"  ⚠ WARNING: '{fn}' is not supported by SDE — approximated as "
                    f"'{replacement}'. Results may differ from Vensim. See LIMITATIONS.md."
                )

    # Run compilation inside ~/.sde_env/compile-workspace/ so that Node's
    # standard upward module resolution finds ~/.sde_env/node_modules/.
    # (Node ESM ignores NODE_PATH, so we can't use an arbitrary cwd.)
    ep = env_path()
    compile_ws = ep / "compile-workspace"
    compile_ws.mkdir(parents=True, exist_ok=True)

    # Write support files into the compile workspace
    _write_package_json(compile_ws)

    # If the MDL was patched, write the modified copy into the compile workspace
    # and compile from that copy.  Otherwise use the original path.
    if patch_warnings:
        patched_mdl = compile_ws / "_model_patched.mdl"
        patched_mdl.write_text(patched_text, encoding="utf-8")
        compile_mdl_path = patched_mdl
    else:
        compile_mdl_path = mdl_path

    _generate_sde_config(
        mdl_path=compile_mdl_path,
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
