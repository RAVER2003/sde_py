"""
main.py — Example usage of sde-py.

Run from the repo root:
    python main.py

First-time setup installs SDEverywhere npm packages into ~/.sde_env/.
"""

import sde

# ── 1. First-time setup (idempotent — safe to call every time) ─────────────
print("=" * 60)
print("Step 1: Setup")
print("=" * 60)
sde.setup()

# ── 2. Load the model ─────────────────────────────────────────────────────
print("=" * 60)
print("Step 2: Load model")
print("=" * 60)
mdl_path = r"e:\RND finale\hello-world\model\sample.mdl"
m = sde.Model(mdl_path)

# ── 3. Inspect the model ──────────────────────────────────────────────────
print("=" * 60)
print("Step 3: Model info")
print("=" * 60)
m.info()

# ── 4. Default run ─────────────────────────────────────────────────────────
print("=" * 60)
print("Step 4: Default run")
print("=" * 60)
result = m.run()
print(result)
print("\ntotal_inventory (first 5 time points):")
print(result.total_inventory.head())

# ── 5. Override an input ───────────────────────────────────────────────────
print("=" * 60)
print("Step 5: Run with production_slope=8")
print("=" * 60)
r2 = m.run(production_slope=8, production_start_year=2030)
print(f"Value at 2050: {r2.at(2050)}")

# ── 6. Derived variable ────────────────────────────────────────────────────
print("=" * 60)
print("Step 6: Derived variable")
print("=" * 60)
m.derive("inventory_doubled", lambda r: r.total_inventory * 2)
r3 = m.run(production_slope=5)
print("inventory_doubled at t=2060:", r3.inventory_doubled[2060])

# ── 7. Sensitivity analysis ────────────────────────────────────────────────
print("=" * 60)
print("Step 7: Sensitivity sweep on production_slope")
print("=" * 60)
df = m.sensitivity("production_slope", [1, 3, 5, 7, 10])
print(df.tail())

# ── 8. Reload with a different model ──────────────────────────────────────
# (commented out — uncomment and point to a second .mdl to test hot-swap)
# m.load(r"e:\RND finale\other_model\model.mdl")

print("\nDone. Model will be shut down automatically.")
m.close()
