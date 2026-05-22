"""Extract per-setting descriptions and group labels from config.py.

The plugin's settings page surfaces a tooltip for each knob. To keep the
operator-facing prose in ONE place — close to the actual default value
the engine uses — we parse `config.py`'s source instead of duplicating
the descriptions in the Pydantic schema's `Field(description=...)`.

Format that config.py must follow:

    # === Group label ===
    #
    # (Optional group-level intro. Ignored by this parser; it's just for
    # someone reading the file.)

    # Description of the setting. May span multiple consecutive comment
    # lines — they're concatenated into one paragraph string. Blank lines
    # within the block end the description.
    SETTING_NAME: type = default_value

Rules:
  - The description for a setting is the contiguous block of `#` lines
    IMMEDIATELY preceding its assignment. Blank lines break the block.
  - A `# === ... ===` banner starts a new group; subsequent assignments
    are tagged with that group until the next banner.
  - Lines that are neither comments nor assignments (imports,
    `if os.path.exists(...)` blocks, the env-var override loop)
    reset the description buffer so they don't accidentally become
    the description for some downstream setting.
  - Assignments are matched by a regex that accepts both `NAME: type = v`
    and `NAME = v` forms. Lowercase names are skipped (private to
    config.py — e.g. `name = entry[0]` in the env-var loop).

Why parsing source instead of importlib + inspect:
  Python's import system strips comments. The standard library's
  `inspect` exposes function docstrings but not module-level
  per-variable comments. We need the raw source.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import os
import re
import sys
from collections import OrderedDict
from typing import Optional

# Make the engine's config.py importable as a top-level `config` module
# regardless of load order. Without this, when bitcart loads the plugin
# via `modules.@barebits.liquidityhelper.plugin` and plugin.py imports
# from `.bitcart_plugin.settings_schema` (which imports us), this file
# runs BEFORE liquidityhelper.py's own sys.path bootstrap has a chance
# to. `importlib.import_module("config")` would then fail, _CONFIG_DOCS
# would silently fall back to {}, and the admin UI would render every
# setting in a single "Other" group with no descriptions.
#
# Adding the plugin's parent dir to sys.path here is idempotent (we
# skip if it's already there) and matches what liquidityhelper.py does
# for the same reason — duplicating the bootstrap is fine because both
# point to the same path.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# Matches a section banner like `# === Liquidity targets ===`. Requires
# the captured label to contain at least one letter — this distinguishes
# a real banner from the all-`=` framing lines we put above and below
# each banner for visual emphasis (those would otherwise match with the
# capture being just `=`).
_BANNER_RE = re.compile(r"^\s*#\s*=+\s+([^=].*?[^=])\s+=+\s*$")

# Matches a comment line that ISN'T a banner. Group 1 captures the body
# (with the leading `# ` stripped).
_COMMENT_RE = re.compile(r"^\s*#\s?(.*)$")

# Matches a top-level assignment to an UPPER_SNAKE_CASE name. We don't
# care about the type annotation or default value — only the name and
# that the line IS an assignment-like statement. Group 1 captures the
# setting name.
_ASSIGNMENT_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=\s*\S"
)


@dataclasses.dataclass(frozen=True)
class SettingDoc:
    """Documentation extracted from config.py for one setting."""

    name: str
    description: str   # Possibly empty if no comment block precedes it.
    group: Optional[str]
    line: int          # Line number in config.py (1-based) of the assignment.


def parse_config_source(source: str) -> "OrderedDict[str, SettingDoc]":
    """Walk `source` line by line, return an OrderedDict mapping
    setting name → SettingDoc. Order matches order of appearance in
    the file, so the admin UI can preserve a deliberate ordering."""
    out: OrderedDict[str, SettingDoc] = OrderedDict()
    current_group: Optional[str] = None
    comment_buffer: list[str] = []
    inside_docstring = False

    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped_for_docstring = line.strip()
        # Skip module/function docstrings; they look like comments but
        # are string literals, not real comments. A naive triple-quote
        # tracker handles the common single-block module docstring at
        # the top of config.py.
        if stripped_for_docstring.startswith('"""') or stripped_for_docstring.startswith("'''"):
            quote = stripped_for_docstring[:3]
            # Toggle on opening; if the same line closes (e.g. `"""x"""`),
            # don't enter docstring mode.
            if stripped_for_docstring.count(quote) >= 2 and stripped_for_docstring != quote:
                continue  # single-line docstring, doesn't change state
            inside_docstring = not inside_docstring
            continue
        if inside_docstring:
            continue
        stripped = line.strip()

        # Section banner: switch group, drop any pending comment block
        # (it was the banner's "intro paragraph", not a setting desc).
        banner_match = _BANNER_RE.match(line)
        if banner_match:
            current_group = banner_match.group(1)
            comment_buffer = []
            continue

        # Plain comment line: accumulate into the buffer.
        comment_match = _COMMENT_RE.match(line)
        if comment_match:
            comment_buffer.append(comment_match.group(1))
            continue

        # Blank line: ends the comment block (matches the "blank line
        # between description and the next thing" convention). The
        # buffer is preserved across one blank but flushed if we then
        # encounter non-comment, non-assignment content. Simplest rule:
        # blank line resets the buffer. That matches what readers
        # naturally expect.
        if not stripped:
            comment_buffer = []
            continue

        # Assignment line: pull the buffered comments as the description.
        assign_match = _ASSIGNMENT_RE.match(line)
        if assign_match:
            name = assign_match.group(1)
            description = _normalize(comment_buffer)
            out[name] = SettingDoc(
                name=name,
                description=description,
                group=current_group,
                line=lineno,
            )
            comment_buffer = []
            continue

        # Anything else (import, if-statement, blank-after-noise):
        # reset the buffer so we don't carry stray comments forward.
        comment_buffer = []

    return out


def _normalize(comment_lines: list[str]) -> str:
    """Join the captured comment lines into one description string.

    Conventions:
      - Strip trailing whitespace on each line.
      - Collapse the lines with single spaces (operator tooltips read
        better as one wrapped paragraph than as a list of short lines).
      - Skip leading empty lines.
    """
    # Drop leading empties (rare, but happens if someone leaves a `# `
    # only line at the top of the block).
    while comment_lines and not comment_lines[0].strip():
        comment_lines.pop(0)
    if not comment_lines:
        return ""
    return " ".join(ln.strip() for ln in comment_lines if ln.strip())


def parse_config_module() -> "OrderedDict[str, SettingDoc]":
    """Convenience: locate the engine's config.py via importlib, read
    its source, and return the parsed map. Called once at schema
    import time; result is cached by the caller."""
    config_mod = importlib.import_module("config")
    source_path = inspect.getsourcefile(config_mod)
    if not source_path:
        return OrderedDict()
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()
    return parse_config_source(source)
