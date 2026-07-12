"""Diff engine and edit-operation helpers.

Pure functions (string in → OT operations out) with no Overleaf API dependencies.
Designed for independent unit testing.
"""

from __future__ import annotations

import logging
import unicodedata
from array import array
from typing import Any

from diff_match_patch import diff_match_patch

from sharelatex_mcp.errors import EditMatchError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 2 * 1024 * 1024       # 2 MB
MAX_DIFF_OPS = 2000                    # fall back to full-replace beyond this
MAX_EDITS_PER_CALL = 100
MAX_OLD_LENGTH = 10 * 1024             # 10 KB
MAX_NEW_LENGTH = 500 * 1024            # 500 KB

# ---------------------------------------------------------------------------
# Diff-compute entry point
# ---------------------------------------------------------------------------


def compute_diff_operations(old: str, new: str) -> list[dict[str, Any]]:
    """Compute minimal sharejs-text-ot operations from *old* to *new*.

    Uses Myers diff (diff-match-patch).  Returns ``[]`` when identical.
    If the computed diff exceeds ``MAX_DIFF_OPS``, falls back to a single
    full-replacement (delete-all + insert-all).

    *Pre-scan heuristic*: if the two inputs appear >90 % different (by
    sampling every 100 bytes), skip the Myers diff entirely and return a
    full-replacement immediately to avoid pathological O(N^2) behaviour.
    """
    if old == new:
        return []

    # Pre-scan: if contents are very different, go straight to full-replace
    if _likely_full_replace(old, new):
        return _make_full_replace(old, new)

    dmp = diff_match_patch()
    diffs = dmp.diff_main(old, new)
    dmp.diff_cleanupMerge(diffs)

    ops: list[dict[str, Any]] = []
    position = 0

    for op, text in diffs:
        if not text:
            continue
        if op == 0:          # EQUAL
            position += len(text)
        elif op == -1:        # DELETE
            ops.append({"p": position, "d": text})
        elif op == 1:         # INSERT
            ops.append({"p": position, "i": text})
            position += len(text)

    if len(ops) > MAX_DIFF_OPS:
        return _make_full_replace(old, new)

    return ops


# ---------------------------------------------------------------------------
# Full-replacement helper
# ---------------------------------------------------------------------------


def _make_full_replace(old: str, new: str) -> list[dict[str, Any]]:
    """Return operations that delete *old* entirely and insert *new*."""
    if not new:
        return [{"p": 0, "d": old}]
    if not old:
        return [{"p": 0, "i": new}]
    return [{"p": 0, "d": old}, {"p": 0, "i": new}]


# ---------------------------------------------------------------------------
# Pre-scan heuristic – avoid pathological O(N²) Myers diff
# ---------------------------------------------------------------------------


def _likely_full_replace(old: str, new: str) -> bool:
    """Return True when *old* and *new* are so different that a full-replace
    is the most efficient strategy anyway.

    Samples every 100 bytes; if >90 % of sampled segments differ, skip Myers.
    Disabled for strings shorter than 500 bytes (sampling is unreliable at
    small sizes, and the Myers diff is fast enough anyway).
    """
    if len(old) < 500 or len(new) < 500:
        return False

    step = 100
    total_checks = 0
    diff_checks = 0

    max_len = min(len(old), len(new))
    for i in range(0, max_len, step):
        total_checks += 1
        end = min(i + 20, max_len)
        if old[i:end] != new[i:end]:
            diff_checks += 1

    len_diff = abs(len(old) - len(new))
    if len_diff > 0 and diff_checks > 0:
        # Only penalise length difference when content already diverges;
        # pure append/prepend should NOT trigger full-replace.
        diff_checks += min(len_diff // step, total_checks // 2)

    return diff_checks > total_checks * 0.9


# ---------------------------------------------------------------------------
# UTF-16 position conversion
# ---------------------------------------------------------------------------


def convert_ot_positions_to_utf16(operations: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Convert all ``p`` fields from code-point to UTF-16 code-unit offsets.

    Operations are applied **sequentially** in sharejs-text-ot: each
    operation's position is relative to the document state after all
    previous operations.  This function correctly handles sequential
    positions by simulating each operation's effect on the text.

    For large diffs we degrade to an estimation-based path
    (1 code-point ≈ 1 UTF-16 code-unit for positions beyond the
    original text), which is exact for ASCII and a safe approximation
    for the mixed case.
    """
    if not operations:
        return operations

    # Pre-compute UTF-16 offset map for the original text
    orig_offsets = array("I", [0]) * (len(text) + 1)
    off = 0
    for i, ch in enumerate(text):
        off += 1 if ord(ch) <= 0xFFFF else 2
        orig_offsets[i + 1] = off

    # When the text-and-operations product is small, use the exact
    # sequential-simulation path.  Otherwise fall back to the fast
    # estimate path, which is still safe (it will never IndexError).
    use_exact = (len(text) * len(operations)) <= 500_000

    if use_exact:
        return _convert_sequential_exact(operations, text)
    else:
        return _convert_estimate(operations, orig_offsets, text, off)


def _convert_sequential_exact(operations: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Exact sequential-simulation path — correct for every input, O(n*m)."""
    current_text = text
    for op in operations:
        p = op["p"]
        limit = min(p, len(current_text))
        utf16_p = 0
        for i in range(limit):
            utf16_p += 1 if ord(current_text[i]) <= 0xFFFF else 2
        if p > len(current_text):
            utf16_p += (p - len(current_text))
        op["p"] = utf16_p
        if "d" in op:
            current_text = current_text[:p] + current_text[p + len(op["d"]):]
        elif "i" in op:
            current_text = current_text[:p] + op["i"] + current_text[p:]
    return operations


def _convert_estimate(
    operations: list[dict[str, Any]],
    orig_offsets: array[int],
    text: str,
    total_utf16: int,
) -> list[dict[str, Any]]:
    """Estimation path — expand lookup to max position, estimate beyond text."""
    max_pos = len(text)
    for op in operations:
        if "p" in op and op["p"] > max_pos:
            max_pos = op["p"]
    if max_pos > len(text):
        offsets = array("I", [0]) * (max_pos + 1)
        for i in range(len(text) + 1):
            offsets[i] = orig_offsets[i]
        for pos in range(len(text) + 1, max_pos + 1):
            offsets[pos] = total_utf16 + (pos - len(text))
        for op in operations:
            if "p" in op:
                op["p"] = offsets[op["p"]]
        return operations
    # max_pos == len(text): reuse precomputed table
    for op in operations:
        if "p" in op:
            op["p"] = orig_offsets[op["p"]]
    return operations


# ---------------------------------------------------------------------------
# Edit operation helpers
# ---------------------------------------------------------------------------


def find_first_two_occurrences(text: str, pattern: str) -> tuple[int, int | None]:
    """Return positions of the first two occurrences of *pattern* in *text*.

    Returns ``(first, None)`` when *pattern* appears exactly once.
    Returns ``(-1, None)`` when not found.
    Raises ``EditMatchError`` when *pattern* is empty.
    """
    if not pattern:
        raise EditMatchError("edit.old must not be empty")
    first = text.find(pattern)
    if first == -1:
        return (-1, None)
    second = text.find(pattern, first + len(pattern))
    if second == -1:
        return (first, None)
    return (first, second)


def sort_edits_by_position(edits: list[dict[str, str]], current: str, *, reverse: bool = True) -> list[dict[str, str]]:
    """Sort *edits* by the position of ``old`` in *current*.

    Each ``old`` must appear exactly once in *current* — this function
    performs the first uniqueness check **and** sorts in one pass.

    *edits* should already have ``old`` / ``new`` in NFC form.
    *current* is the raw (unnormalised) text from Overleaf.
    """
    positions: list[tuple[int, dict[str, str]]] = []
    for idx, edit in enumerate(edits):
        old_nfc = edit["old"]
        first, second = find_first_two_occurrences(current, old_nfc)
        if first == -1:
            raise EditMatchError(
                f'edit.old "{old_nfc[:50]}..." not found in file (0 matches)',
                edit_index=idx,
                edit=edit,
            )
        if second is not None:
            raise EditMatchError(
                f'edit.old "{old_nfc[:50]}..." matched ≥2 locations, must be unique',
                edit_index=idx,
                edit=edit,
            )
        positions.append((first, edit))

    positions.sort(key=lambda x: x[0], reverse=reverse)
    return [edit for _, edit in positions]


def compute_edit_operations(current: str, edits: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Cross-validate and apply a batch of edits, returning OT operations.

    Algorithm (see design doc §5.2):

    1. NFC-normalise only ``old`` / ``new`` — *current* stays raw so that
       OT positions are relative to the same text that Overleaf stores.
    2. Sort edits by original position (descending).
    3. Walk back-to-front through the list, applying each edit on the
       already-modified text and verifying uniqueness.
    4. Compute a single diff from original → modified → OT operations.

    Raises ``EditMatchError`` if any ``old`` has zero or multiple matches.
    """
    if not edits:
        return []

    # Step 0: NFC-normalise old/new only — NOT current
    normalized: list[dict[str, str]] = []
    for e in edits:
        normalized.append({
            "old": unicodedata.normalize("NFC", e["old"]),
            "new": unicodedata.normalize("NFC", e["new"]),
        })
    edits = normalized

    # Step 1: sort + initial uniqueness check on original current
    sorted_edits = sort_edits_by_position(edits, current, reverse=True)

    # Step 2: validate & apply each edit on progressively-modified text
    modified = current
    for edit in sorted_edits:
        if edit["old"] == edit["new"]:
            continue  # identity edit

        first, second = find_first_two_occurrences(modified, edit["old"])
        if first == -1:
            raise EditMatchError(
                f'edit.old "{edit["old"][:50]}..." not found (0 matches after prior edits)',
                edit=edit,
            )
        if second is not None:
            raise EditMatchError(
                f'edit.old "{edit["old"][:50]}..." matched ≥2 locations',
                edit=edit,
            )

        modified = modified[:first] + edit["new"] + modified[first + len(edit["old"]):]

    # Step 3: single diff → OT batch
    return compute_diff_operations(current, modified)


# ---------------------------------------------------------------------------
# edit retry idempotency check
# ---------------------------------------------------------------------------


def check_edits_already_applied(current: str, edits: list[dict[str, str]]) -> bool:
    """Return ``True`` if *current* already reflects all *edits* being applied.

    Used during OT retry when the ack was lost but the operation may have
    succeeded (design doc §7.3).  Strategy: apply the edits naively to
    *current* (without uniqueness validation — the edits have already been
    applied, so ``old`` strings should be absent).  If the result is
    identical to *current*, the edits were already present.  This avoids
    false positives from ``new_text`` that coincidentally exists elsewhere.
    """
    if not edits:
        return False

    modified = current
    any_change = False
    for edit in edits:
        old_text = unicodedata.normalize("NFC", edit["old"])
        new_text = unicodedata.normalize("NFC", edit["new"])
        if old_text == new_text:
            continue

        pos = modified.find(old_text)
        if pos != -1:
            # old_text still present → edits were NOT applied (or only partially)
            return False
        any_change = True

    # If no old_text was found for any non-identity edit, and we found at
    # least one non-identity edit, consider the edits already applied.
    # If ALL edits were identity, return False (not "already applied").
    return any_change
