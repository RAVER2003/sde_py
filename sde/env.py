"""
env.py — Global npm environment manager for sde-py.

Installs @sdeverywhere/* packages into ~/.sde_env/ once.
All sde-py projects share this environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def env_path() -> Path:
    """Return the global sde environment directory (~/.sde_env)."""
    return Path.home() / ".sde_env"


def is_setup() -> bool:
    """Return True if the npm environment has been installed."""
    return (env_path() / "node_modules" / "@sdeverywhere").is_dir()


def setup(verbose: bool = True) -> None:
    """
    Install SDEverywhere npm packages into ~/.sde_env/ and copy node_runner.mjs there.
    Safe to call multiple times — skips if already installed.
    """
    ep = env_path()
    ep.mkdir(parents=True, exist_ok=True)

    # Write package.json for the global environment
    pkg = {
        "name": "sde-env",
        "version": "1.0.0",
        "private": True,
        "type": "module",
        "dependencies": {
            "@sdeverywhere/cli": "*",
            "@sdeverywhere/build": "*",
            "@sdeverywhere/runtime": "*",
            "@sdeverywhere/plugin-worker": "*",
        },
    }
    pkg_path = ep / "package.json"
    pkg_path.write_text(json.dumps(pkg, indent=2))

    if verbose:
        print("Installing SDEverywhere npm packages into ~/.sde_env/ ...")

    result = subprocess.run(
        "npm install --prefer-offline",
        cwd=ep,
        capture_output=not verbose,
        text=True,
        shell=True,
    )
    if result.returncode != 0:
        err = result.stderr if result.stderr else "npm install failed"
        raise RuntimeError(f"npm install failed:\n{err}")

    # Copy the bundled node_runner.mjs into the env
    data_dir = Path(__file__).parent / "_data"
    runner_src = data_dir / "node_runner.mjs"
    runner_dst = ep / "node_runner.mjs"
    shutil.copy2(runner_src, runner_dst)

    if verbose:
        print("✓ Setup complete. SDEverywhere is ready.")


def setup_cli() -> None:
    """Entry point for the `sde-setup` console script."""
    try:
        setup(verbose=True)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def require_setup() -> Path:
    """Raise a helpful error if setup has not been run. Returns env_path() if OK."""
    ep = env_path()
    if not is_setup():
        raise RuntimeError(
            "SDEverywhere environment not found. Run `sde.setup()` or `sde-setup` first."
        )
    return ep


def node_runner_path() -> Path:
    """Return the path to the installed node_runner.mjs."""
    return env_path() / "node_runner.mjs"


def sde_cli_path() -> Path:
    """Return the path to the sde CLI .js entry point inside the npm env.

    Reads @sdeverywhere/cli/package.json to find the actual bin entry rather
    than hard-coding a path that may differ between versions.
    """
    import json as _json

    cli_pkg_dir = env_path() / "node_modules" / "@sdeverywhere" / "cli"
    if not cli_pkg_dir.is_dir():
        raise FileNotFoundError(
            f"@sdeverywhere/cli not found at {cli_pkg_dir}. Run `sde.setup()` first."
        )

    # Try the well-known path first
    well_known = cli_pkg_dir / "bin" / "sde.js"
    if well_known.exists():
        return well_known

    # Fall back: read package.json → bin field
    pkg_json = cli_pkg_dir / "package.json"
    if pkg_json.exists():
        pkg = _json.loads(pkg_json.read_text(encoding="utf-8"))
        bin_field = pkg.get("bin", {})
        if isinstance(bin_field, str):
            candidate = cli_pkg_dir / bin_field
        elif isinstance(bin_field, dict):
            # Take the first entry (usually "sde")
            first = next(iter(bin_field.values()), None)
            candidate = cli_pkg_dir / first if first else None
        else:
            candidate = None

        if candidate and candidate.exists():
            return candidate

    # Last resort: walk bin/ for any .js file
    bin_dir = cli_pkg_dir / "bin"
    if bin_dir.is_dir():
        for f in sorted(bin_dir.iterdir()):
            if f.suffix == ".js":
                return f

    raise FileNotFoundError(
        f"Could not locate sde CLI entry point inside {cli_pkg_dir}. "
        "Try running `sde.setup()` again."
    )
