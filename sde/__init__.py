"""
sde — Python SDK for SDEverywhere.

Quickstart::

    import sde

    sde.setup()                          # first-time: installs npm packages (~1 min)

    m = sde.Model('path/to/model.mdl')  # parse, compile, start Node runner
    m.info()

    result = m.run(production_slope=5)
    print(result.total_inventory)
"""

from .env import setup, is_setup, env_path
from .model import Model

__all__ = ["Model", "setup", "is_setup", "env_path"]
