"""Tests for the config.py → tooltip pipeline.

Covers:
  - parse_config_source: the line-walker correctly captures
    description blocks, banner-based groups, and ignores docstrings.
  - parse_config_module: the real config.py round-trips through the
    parser cleanly; every setting in PluginSettings has a parsed
    description.
  - Drift detection: descriptions don't accidentally become very short
    (stub) or very long (lost paragraph break).
  - get_settings_groups: returns groups in declaration order, each
    setting in exactly one group.
"""

from __future__ import annotations

import textwrap

import pytest

from bitcart_plugin.config_doc_parser import (
    SettingDoc,
    parse_config_module,
    parse_config_source,
)
from bitcart_plugin.settings_schema import (
    PluginSettings,
    SETTING_NAMES,
    get_settings_groups,
)


# ---------------------------------------------------------------------------
# parse_config_source — unit tests for the line-walker
# ---------------------------------------------------------------------------

def test_simple_setting_with_description_and_group():
    """The basic shape: banner, blank line, description, assignment.
    The description and group should both attach to the setting."""
    source = textwrap.dedent("""
        # === Liquidity targets ===

        # Description of the setting.
        # Wraps across two comment lines.
        MIN_THING: int = 5
    """).strip()
    docs = parse_config_source(source)
    assert "MIN_THING" in docs
    doc = docs["MIN_THING"]
    assert doc.group == "Liquidity targets"
    assert doc.description == "Description of the setting. Wraps across two comment lines."


def test_setting_without_description():
    """A setting with no comment block above it must still be captured,
    just with an empty description. We don't want missing prose to
    silently drop the setting from the schema."""
    source = textwrap.dedent("""
        # === Group ===

        SETTING_A = 1
    """).strip()
    docs = parse_config_source(source)
    assert "SETTING_A" in docs
    assert docs["SETTING_A"].description == ""


def test_banner_only_no_settings_yields_empty():
    """No assignments → no docs."""
    source = "# === Empty ===\n"
    docs = parse_config_source(source)
    assert docs == {}


def test_blank_line_breaks_description_block():
    """Description is only the contiguous comment block IMMEDIATELY
    above the assignment. A blank line breaks the association."""
    source = textwrap.dedent("""
        # === Group ===

        # This is NOT the description (blank line below resets it).

        # This IS the description.
        SETTING: int = 1
    """).strip()
    docs = parse_config_source(source)
    assert docs["SETTING"].description == "This IS the description."


def test_non_comment_non_assignment_resets_buffer():
    """An import statement or other code between a comment block and
    the assignment must reset the description buffer."""
    source = textwrap.dedent("""
        # === Group ===

        # Stale comment.
        import os

        SETTING: int = 1
    """).strip()
    docs = parse_config_source(source)
    assert docs["SETTING"].description == ""


def test_subsequent_banner_changes_group():
    """Settings after a new banner pick up the new group label."""
    source = textwrap.dedent("""
        # === First ===

        # A
        SETTING_A: int = 1

        # === Second ===

        # B
        SETTING_B: int = 2
    """).strip()
    docs = parse_config_source(source)
    assert docs["SETTING_A"].group == "First"
    assert docs["SETTING_B"].group == "Second"


def test_lowercase_assignment_is_skipped():
    """Lowercase names are private/internal (e.g. loop variables in
    the env-var override loop in config.py). They must not appear in
    the docs map."""
    source = textwrap.dedent("""
        # === Group ===

        # Real setting.
        REAL_SETTING: int = 1

        # Shouldn't appear.
        private_var = "stuff"
    """).strip()
    docs = parse_config_source(source)
    assert "REAL_SETTING" in docs
    assert "private_var" not in docs


def test_all_equals_framing_lines_dont_clobber_group():
    """The visual `# ======================` framing lines used above
    and below banners must NOT match as banners themselves (else
    group becomes '=' or empty)."""
    source = textwrap.dedent("""
        # =============================================================================
        # === Real Group ===
        # =============================================================================

        # Description.
        SETTING: int = 1
    """).strip()
    docs = parse_config_source(source)
    assert docs["SETTING"].group == "Real Group"


def test_docstring_section_examples_dont_become_real_banners():
    """The module docstring at the top of config.py contains an
    example `# === Section name ===` line. The parser must skip
    docstring content so that example doesn't become an active group."""
    source = textwrap.dedent('''
        """Module docstring.

        Conventions:
          # === Example section ===

          # Example description.
          EXAMPLE_NAME = 1
        """
        from typing import Optional

        # === Real Section ===

        # Real description.
        REAL_SETTING: int = 1
    ''').strip()
    docs = parse_config_source(source)
    # Settings inside the docstring must NOT have been captured.
    assert "EXAMPLE_NAME" not in docs
    # The real setting should land in the real group.
    assert docs["REAL_SETTING"].group == "Real Section"


# ---------------------------------------------------------------------------
# parse_config_module — integration with the actual config.py
# ---------------------------------------------------------------------------

def test_every_schema_field_has_a_parsed_description():
    """Every public setting in PluginSettings must have a non-empty
    description after parsing config.py. Catches the case where someone
    adds a field to the schema but forgets to document it in config.py."""
    docs = parse_config_module()
    missing = []
    for name in SETTING_NAMES:
        doc = docs.get(name)
        if doc is None:
            missing.append(f"{name}: not found in config.py")
        elif not doc.description.strip():
            missing.append(f"{name}: empty description in config.py")
    assert not missing, "Settings missing descriptions:\n  " + "\n  ".join(missing)


def test_every_schema_field_has_a_parsed_group():
    """Every public setting must live under a section banner. Catches
    a setting being added at the top of config.py before any banner."""
    docs = parse_config_module()
    ungrouped = []
    for name in SETTING_NAMES:
        doc = docs.get(name)
        if doc and doc.group is None:
            ungrouped.append(name)
    assert not ungrouped, (
        f"Settings outside any group: {ungrouped}. Move them under a "
        f"`# === Group ===` banner in config.py."
    )


def test_descriptions_are_within_sensible_length():
    """Drift detector: tooltips that are too short are stubs ('TODO:
    describe this'); tooltips that are too long don't fit in the UI.
    Cap at a reasonable range. Catches accidental paste-the-whole-RFC
    descriptions and lazy one-word stubs alike."""
    docs = parse_config_module()
    too_short, too_long = [], []
    for name in SETTING_NAMES:
        doc = docs.get(name)
        if not doc or not doc.description:
            continue
        if len(doc.description) < 25:
            too_short.append(f"{name}: {len(doc.description)} chars")
        if len(doc.description) > 800:
            too_long.append(f"{name}: {len(doc.description)} chars")
    assert not too_short, "Stub-like descriptions:\n  " + "\n  ".join(too_short)
    assert not too_long, "Excessively long descriptions:\n  " + "\n  ".join(too_long)


def test_field_descriptions_are_patched_from_config(monkeypatch):
    """The schema's Field(description=...) must actually reflect the
    parsed config.py prose, not the hand-written fallback. End-to-end
    pin of the wiring."""
    docs = parse_config_module()
    # Pick a setting with a known description in config.py.
    name = "MIN_CHANNEL_COUNT"
    parsed = docs[name].description
    assert parsed, "config.py is missing the test fixture description"
    schema_desc = PluginSettings.model_fields[name].description
    assert schema_desc == parsed, (
        f"Schema description for {name} doesn't match config.py:\n"
        f"  config.py: {parsed!r}\n"
        f"  schema:    {schema_desc!r}"
    )


def test_get_settings_groups_is_ordered_and_complete():
    """get_settings_groups must:
      - return groups in declaration order (not alphabetical),
      - include EVERY public setting exactly once,
      - have no empty groups.
    """
    groups = get_settings_groups()
    seen: set[str] = set()
    for group, names in groups:
        assert names, f"empty group: {group!r}"
        for n in names:
            assert n not in seen, f"setting {n!r} appears in multiple groups"
            seen.add(n)
    # Every public schema field must be in some group.
    missing = SETTING_NAMES - seen
    assert not missing, f"settings not assigned to any group: {sorted(missing)}"


def test_get_settings_groups_excludes_internal_legacy():
    """Internal/legacy constants (FEE_PAYOUT_REASON, TOPUP_*) are
    documented in config.py for completeness but deliberately NOT in
    the schema. They must not appear in the UI group list either —
    the schema is the gate."""
    groups = get_settings_groups()
    all_listed = set()
    for _, names in groups:
        all_listed.update(names)
    forbidden = {"FEE_PAYOUT_REASON", "CASHOUT_REASON", "TOPUP_NAME",
                 "TOPUP_BAREBITS", "MIN_CHANNEL_SIZE_IN_SATS"}
    leaked = forbidden & all_listed
    assert not leaked, (
        f"Internal constants leaked into UI groups: {leaked}. They live "
        f"in config.py for documentation but should not be in the schema."
    )
