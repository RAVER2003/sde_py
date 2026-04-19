"""
parser.py — Parse a Vensim .mdl file to extract variable metadata.

Modelled after SDEverywhere's preprocess-vensim.ts (packages/parse/src/vensim/).
Key steps that mirror SDE's preprocessor:
  1. Strip {UTF-8} / inline {…} comments
  2. Stop at the sketch section (\\---///)
  3. Remove macro blocks (:MACRO: … :END OF MACRO:)
  4. Join backslash continuation lines
  5. Split on '|' to get individual definition blocks
  6. For each block: split on '~', reduceWhitespace on each part
  7. Classify variables: inputs (have [min,max] range), constants (plain numbers),
     outputs (everything else that has an equation)

Extracts:
  - inputs: variables with a [min,max] or [min,max,step] range annotation
  - outputs: computed variables (have equations, no range annotation)
  - constants: named values (no range, no complex equation)
  - time: INITIAL TIME, FINAL TIME, TIME STEP, SAVEPER
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ── Name conversion ─────────────────────────────────────────────────────────

def to_python_name(vensim_name: str) -> str:
    """Convert a Vensim variable name to a Python-friendly snake_case identifier.

    Mirrors the algorithm in SDE's canonical-id.js but without the leading '_'.

    'Production start year' → 'production_start_year'
    'Total inventory'       → 'total_inventory'
    'Capacity (units)'      → 'capacity__units_'
    """
    name = vensim_name.strip()
    # Replace one or more consecutive whitespace or underscore chars with single '_'
    # (matches Vensim's documented variable-name rules, same as SDE)
    name = re.sub(r"[\s_]+", "_", name)
    # Replace any remaining non-alphanumeric characters with '_'
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    # Collapse multiple underscores and strip leading/trailing
    name = re.sub(r"_+", "_", name).strip("_")
    return name.lower()


def to_vensim_name(python_name: str, lookup: dict[str, str]) -> str | None:
    """Reverse-lookup from python_name → Vensim name using a pre-built mapping."""
    return lookup.get(python_name)


# ── Text preprocessing (mirrors SDE's preprocess-vensim.ts) ─────────────────

def _strip_encoding_header(text: str) -> str:
    """Remove the {UTF-8} or other encoding declaration at the top of the file."""
    return re.sub(r"^\{[^}]*\}\s*", "", text)


def _strip_inline_comments(text: str) -> str:
    """Remove inline {…} comments from a block of text.

    Mirrors SDE's replaceDelimitedStrings(input, '{', '}', '').
    """
    result = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
        elif depth == 0:
            result.append(ch)
    return "".join(result)


def _process_backslashes(text: str) -> str:
    """Join lines separated by a backslash continuation character.

    Mirrors SDE's processBackslashes(). A line ending with '\\' is joined
    to the next line (the backslash and the leading whitespace of the next
    line are replaced by a single space).
    """
    lines = text.splitlines()
    output_lines: list[str] = []
    pending = ""
    for line in lines:
        if pending:
            line = pending + line.strip()
            pending = ""
        if line.rstrip().endswith("\\"):
            # Strip the trailing backslash and trailing whitespace
            pending = line.rstrip()[:-1].rstrip() + " "
        else:
            output_lines.append(line)
    if pending:
        output_lines.append(pending)
    return "\n".join(output_lines)


def _reduce_whitespace(text: str) -> str:
    """Collapse runs of whitespace (including newlines/tabs) to a single space.

    Mirrors SDE's reduceWhitespace(). Used to produce a single-line
    representation of each equation/units/comment part.
    """
    return re.sub(r"\s{2,}", " ", text).strip()


def _remove_macros(text: str) -> str:
    """Remove :MACRO: … :END OF MACRO: blocks.

    Mirrors SDE's removeMacros(). We replace the entire block with blank
    lines so that parsing of subsequent blocks is not affected.
    """
    def _replace(m: re.Match) -> str:
        # Preserve line count so that subsequent blocks keep correct line numbers
        return "\n" * m.group(0).count("\n")

    return re.sub(r":MACRO:.*?:END OF MACRO:", _replace, text, flags=re.DOTALL)


# ── Block splitting ──────────────────────────────────────────────────────────

_GROUP_MARKER_RE = re.compile(r"^\s*\*{4,}", re.MULTILINE)
_SKETCH_MARKER = r"\---///"


def _split_on_pipe(text: str) -> list[str]:
    """Split *text* on '|' (Vensim block separator) while respecting quoted strings.

    Vensim allows quoted variable names that may contain a literal '|', e.g.:
        "quotes 3 with | pipe character "  = 2
    A naive ``text.split('|')`` would break the name.  We track double-quote
    pairing and only split on '|' that are outside any quoted region.
    """
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == "|" and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _split_blocks(text: str) -> list[str]:
    """Split the .mdl file into individual variable-definition blocks.

    Mirrors the logic in SDE's splitDefs():
      - Strip encoding header and inline comments
      - Remove macro blocks
      - Stop at the private sketch section
      - Split on '|' (the Vensim block terminator)
      - Skip group-header blocks (lines of '*')
    """
    text = _strip_encoding_header(text)

    # Stop at the sketch section — nothing after it is a variable definition
    sketch_idx = text.find(_SKETCH_MARKER)
    if sketch_idx >= 0:
        text = text[:sketch_idx]

    # Remove macro blocks
    text = _remove_macros(text)

    # Split on '|' — the Vensim block separator, but NOT inside quoted strings.
    # Quoted variable names (e.g. "quotes 3 with | pipe character") may contain
    # literal '|' characters that must not be treated as block separators.
    raw_blocks = _split_on_pipe(text)

    blocks: list[str] = []
    for block in raw_blocks:
        stripped = block.strip()
        if not stripped:
            continue
        # Skip group-header blocks (lines of asterisks like ****...****)
        if _GROUP_MARKER_RE.match(stripped):
            continue
        blocks.append(stripped)

    return blocks


# ── Block parsing ────────────────────────────────────────────────────────────

_RANGE_RE = re.compile(
    r"\[\s*(-?[\d.eE+\-]+)\s*,\s*(-?[\d.eE+\-]+)"
    r"(?:\s*,\s*(-?[\d.eE+\-]+))?\s*\]"
)
# Match "LHS = RHS" — LHS must not contain '='
_ASSIGNMENT_RE = re.compile(r"^([^=]+?)\s*=\s*(.*)", re.DOTALL)

# Numeric-array RHS: only digits, whitespace, commas, semicolons, signs, 'e'/'E'
# Used to detect constant arrays like "inputA[DimA] = -1, +2, 3"
_NUMERIC_ARRAY_RHS_RE = re.compile(r"^[0-9eE+\-.,;\s]+$")


def _is_numeric_array_rhs(rhs: str) -> bool:
    """Return True if rhs looks like a multi-value numeric array constant.

    Requires at least one comma or semicolon separating values, e.g. ``'-1, +2, 3'``
    or ``'11,12; 21,22'``.  A bare single number such as ``'1'`` is NOT a
    numeric array — it represents an element-specific constant definition
    (e.g. ``ce[t1] = 1``) and must be classified as an output, not skipped.
    """
    s = rhs.strip()
    if not s or not any(c.isdigit() for c in s):
        return False
    # A numeric array must have at least one value separator (comma or semicolon).
    if "," not in s and ";" not in s:
        return False
    return bool(_NUMERIC_ARRAY_RHS_RE.match(s))


# Time-control variable names (case-insensitive, strip whitespace before matching)
_TIME_VARS = {"initial time", "final time", "time step", "saveper"}

# Dimension definition lines: "DimA: A1, A2, A3" or "DimA -> DimB: ..."
# They have a colon but no '=' sign.
_DIMENSION_DEF_RE = re.compile(r"^([A-Za-z][\w'\s]*)(?:\s*->\s*[A-Za-z][\w'\s]*)?\s*:\s*")


def _collect_dimension_names(blocks: list[str]) -> tuple[set[str], set[str]]:
    """Return (all_dimension_names, root_dimension_names).

    Dimension definitions look like::

        DimA: A1, A2, A3
        DimA -> DimB: A1, A2, A3
        XPriority : ptype, ppriority, pwidth, pextra

    They have a colon and NO '=' sign.

    Root dimensions are those whose element set is NOT a proper subset of any
    other dimension's elements.  Sub-range dimensions (e.g. ``SubA`` whose
    elements {A2, A3} ⊂ ``DimA``'s {A1, A2, A3}) are excluded from
    ``root_names`` because SDE cannot resolve subscripted variables by bare
    name in spec.json when the subscript is a sub-range rather than the full
    parent dimension.
    """
    dim_elements: dict[str, frozenset[str]] = {}

    for block in blocks:
        stripped = block.strip()
        if "=" in stripped:
            continue  # assignments are not dimension defs
        m = _DIMENSION_DEF_RE.match(stripped)
        if m:
            name = m.group(1).strip().lower()
            after_colon = stripped[m.end():]
            # The raw block includes ~-separated units/comment parts; take only
            # the elements part (before the first tilde).
            after_colon = after_colon.split("~")[0].strip()
            # Strip any trailing mapping clause "-> ..." so we only keep the
            # comma-separated element names.
            after_colon = re.sub(r"\s*->.*", "", after_colon).strip()
            elements = frozenset(
                e.strip().lower() for e in after_colon.split(",") if e.strip()
            )
            if elements:
                dim_elements[name] = elements

    all_names = set(dim_elements.keys())

    # A dimension is excluded from root_names if:
    #  (a) it is a sub-range (its elements are a proper subset of another dim's elements), OR
    #  (b) it is hierarchical (its elements are themselves dimension names — a
    #      "dimension of dimensions" like DimAB: DimAB1, DimAB2, DimAB3).
    # SDE cannot resolve subscripted variables by bare name in spec.json when the
    # subscript falls into either of these categories.
    root_names: set[str] = set()
    for name, elements in dim_elements.items():
        is_sub_range = any(
            elements < other_elements
            for other_name, other_elements in dim_elements.items()
            if other_name != name
        )
        is_hierarchical = any(e in all_names for e in elements)
        if not is_sub_range and not is_hierarchical:
            root_names.add(name)

    return all_names, root_names


def _parse_block(block: str) -> dict[str, Any] | None:
    """Parse a single .mdl block into a metadata dict, or None if unparseable.

    Mirrors SDE's processDef():
      1. Strip inline {…} comments
      2. Join backslash continuation lines
      3. Split on '~' to get [equation_part, units_part, comment_part]
      4. reduceWhitespace on each part
      5. Extract variable name and RHS from equation_part
    """
    # 1. Strip inline comments
    block = _strip_inline_comments(block)

    # 2. Join backslash continuation lines
    block = _process_backslashes(block)

    # 3. Split on '~' separators (handles both single '~' and '~~')
    #    We split on single '~' first since that is the canonical Vensim separator;
    #    '~~' forms are just two consecutive single-tilde delimiters.
    parts = block.split("~")
    if not parts:
        return None

    # 4. Reduce whitespace on each part — this matches SDE's reduceWhitespace()
    #    and ensures equations are stored as clean single-line strings.
    eq_part   = _reduce_whitespace(parts[0])
    units_part = _reduce_whitespace(parts[1]) if len(parts) > 1 else ""
    doc_part   = _reduce_whitespace(parts[2]) if len(parts) > 2 else ""

    # 5. Parse the equation part
    m = _ASSIGNMENT_RE.match(eq_part)
    if not m:
        return None

    var_name = m.group(1).strip()
    rhs = m.group(2).strip()

    # Detect :EXCEPT: clause before stripping — variables using :EXCEPT: are
    # only defined for a subset of their subscript dimension, so SDE cannot
    # resolve them by bare name in spec.json.
    has_except = bool(re.search(r":EXCEPT:", var_name, re.IGNORECASE))

    # Remove subscript dimensions from variable name, e.g. "Var[DimA]" → "Var"
    subscript_match = re.search(r"\[([^\]]+)\]", var_name)
    is_subscripted = subscript_match is not None
    # Extract the subscript tokens so the caller can check if they're all dimensions
    raw_subscripts = (
        [s.strip().lower() for s in subscript_match.group(1).split(",")]
        if subscript_match else []
    )
    var_name_clean = re.sub(r"\[.*?\]", "", var_name).strip()
    # Remove :EXCEPT: clauses (SDE handles these separately, we just strip them)
    var_name_clean = re.sub(r"\s*:EXCEPT:.*$", "", var_name_clean, flags=re.DOTALL).strip()

    if not var_name_clean:
        return None

    # Detect range annotation in units_part → marks this as a slider/input variable
    range_match = _RANGE_RE.search(units_part)
    has_range = range_match is not None

    min_val = max_val = step_val = default_val = None
    if has_range:
        min_val  = float(range_match.group(1))
        max_val  = float(range_match.group(2))
        step_val = float(range_match.group(3)) if range_match.group(3) else None
        try:
            default_val = float(rhs)
        except ValueError:
            default_val = None

    # Detect plain numeric constant (RHS is a bare number, no range annotation,
    # and the variable is NOT subscripted — subscripted variables with numeric RHS
    # are array constants that cannot be used as scalar inputs).
    is_numeric_const = False
    const_val = None
    if not is_subscripted:
        try:
            const_val = float(rhs)
            is_numeric_const = True
        except ValueError:
            pass

    # Detect subscripted numeric-array constants like "inputA[DimA] = -1, +2, 3"
    # and TABBED ARRAY inline data tables like "Pop[DimA,DimB] = TABBED ARRAY(...)".
    # Also detect GET DIRECT LOOKUPS / GET XLS LOOKUPS — these define lookup tables
    # loaded from external files; SDE treats them as internal lookups, not outputs.
    # SDE initialises these internally; they cannot appear in spec.json outputs.
    _rhs_upper = rhs.strip().upper()
    is_numeric_array_const = is_subscripted and (
        _is_numeric_array_rhs(rhs)
        or _rhs_upper.startswith("TABBED ARRAY(")
        or _rhs_upper.startswith("GET DIRECT LOOKUPS(")
        or _rhs_upper.startswith("GET XLS LOOKUPS(")
    )

    return {
        "varName":             var_name_clean,
        "pythonName":          to_python_name(var_name_clean),
        "rhs":                 rhs,
        "isSubscripted":       is_subscripted,
        "rawSubscripts":       raw_subscripts,
        "isNumericArrayConst": is_numeric_array_const,
        "hasExcept":           has_except,
        "hasRange":            has_range,
        "minValue":         min_val,
        "maxValue":         max_val,
        "stepValue":        step_val,
        "defaultValue":     default_val,
        "isNumericConstant": is_numeric_const,
        "constantValue":    const_val,
        "units":            units_part,
        "doc":              doc_part,
    }


# ── Main parse function ──────────────────────────────────────────────────────

def parse_mdl(mdl_path: str | Path) -> dict[str, Any]:
    """
    Parse a Vensim .mdl file and return a metadata dict.

    Returns:
        {
          "inputs":    [ { varName, pythonName, defaultValue, minValue, maxValue, stepValue, doc } ],
          "outputs":   [ { varName, pythonName, equation, doc } ],
          "constants": [ { varName, pythonName, value, doc } ],
          "time":      { initial, final, step, saveper },
          "pyToVensim": { python_name: vensim_name },
          "vensimToPy": { vensim_name: python_name },
        }

    Equations stored in outputs[*]["equation"] are already reduced to a single
    line (whitespace collapsed), matching what SDE's preprocessor produces.
    """
    text = Path(mdl_path).read_text(encoding="utf-8-sig")
    blocks = _split_blocks(text)

    # Collect dimension names first so we can classify subscripts correctly.
    # A subscripted variable whose subscripts are ALL root dimension names can be
    # listed in spec.json; one using element names (like [A1]) or sub-range
    # dimensions (like [SubA] where SubA ⊂ DimA) cannot.
    dimension_names, root_dimension_names = _collect_dimension_names(blocks)

    inputs:    list[dict] = []
    outputs:   list[dict] = []
    constants: list[dict] = []
    time_vars: dict[str, float | None] = {}

    # Track seen python names per category to avoid duplicates from subscripted blocks
    # (e.g. "Var[A1] = 1" and "Var[DimB] = 2" both reduce to the same varName "Var")
    seen_inputs:    set[str] = set()
    seen_outputs:   set[str] = set()
    seen_constants: set[str] = set()

    for block in blocks:
        parsed = _parse_block(block)
        if not parsed:
            continue

        vn = parsed["varName"].lower().strip()

        # Time-control variables (INITIAL TIME, FINAL TIME, TIME STEP, SAVEPER)
        if vn in _TIME_VARS:
            key = re.sub(r"\s+", "_", vn)
            time_vars[key] = parsed["constantValue"]  # None if not a plain number
            continue

        py = parsed["pythonName"]

        # Skip subscripted numeric-array constants (e.g. "inputA[DimA] = -1, +2, 3").
        # SDE initialises them internally; they can't be outputs or scalar inputs.
        if parsed["isNumericArrayConst"]:
            continue

        if parsed["hasRange"]:
            if py not in seen_inputs:
                seen_inputs.add(py)
                inputs.append({
                    "varName":      parsed["varName"],
                    "pythonName":   py,
                    "defaultValue": parsed["defaultValue"],
                    "minValue":     parsed["minValue"],
                    "maxValue":     parsed["maxValue"],
                    "stepValue":    parsed["stepValue"],
                    "doc":          parsed["doc"],
                })
        elif parsed["isNumericConstant"]:
            if py not in seen_constants:
                seen_constants.add(py)
                constants.append({
                    "varName":    parsed["varName"],
                    "pythonName": py,
                    "value":      parsed["constantValue"],
                    "doc":        parsed["doc"],
                })
        else:
            if py not in seen_outputs:
                seen_outputs.add(py)
                # A subscripted output is only safe to list in spec.json when ALL
                # its subscripts are known dimension names (not specific elements).
                # E.g. a[DimA] → ok; Var[A1] or Var[DimAExceptA1 subset] → not ok.
                raw_subs = parsed["rawSubscripts"]
                all_dims = (
                    bool(raw_subs)
                    and all(s in root_dimension_names for s in raw_subs)
                    and not parsed.get("hasExcept", False)
                )
                outputs.append({
                    "varName":        parsed["varName"],
                    "pythonName":     py,
                    "equation":       parsed["rhs"],   # single-line, whitespace-reduced
                    "isSubscripted":  parsed["isSubscripted"],
                    "allDimSubscripts": all_dims,  # True → safe to include in spec.json
                    "doc":            parsed["doc"],
                })

    # Build bidirectional name-mapping dicts
    py_to_vensim: dict[str, str] = {}
    vensim_to_py: dict[str, str] = {}
    for group in (inputs, outputs, constants):
        for v in group:
            py_to_vensim[v["pythonName"]] = v["varName"]
            vensim_to_py[v["varName"]]    = v["pythonName"]

    return {
        "inputs":    inputs,
        "outputs":   outputs,
        "constants": constants,
        "time": {
            "initial": time_vars.get("initial_time"),
            "final":   time_vars.get("final_time"),
            "step":    time_vars.get("time_step"),
            "saveper": time_vars.get("saveper"),
        },
        "pyToVensim": py_to_vensim,
        "vensimToPy": vensim_to_py,
    }
