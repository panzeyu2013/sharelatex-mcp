"""Diff engine and edit-operation helpers.

Pure functions (string in → OT operations out) with no Overleaf API dependencies.
Designed for independent unit testing.
"""

from __future__ import annotations

import logging
from array import array
from dataclasses import dataclass
from typing import Any

from diff_match_patch import diff_match_patch  # type: ignore[import-untyped]

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


@dataclass(frozen=True)
class _TextSource:
    text: str
    utf16_offsets: array[int]

    @classmethod
    def from_text(cls, text: str) -> _TextSource:
        offsets = array("I", [0])
        offset = 0
        for char in text:
            offset += 1 if ord(char) <= 0xFFFF else 2
            offsets.append(offset)
        return cls(text=text, utf16_offsets=offsets)

    def utf16_units(self, start: int, end: int) -> int:
        return self.utf16_offsets[end] - self.utf16_offsets[start]


@dataclass(frozen=True)
class _TextPiece:
    source: _TextSource
    start: int
    end: int

    def __len__(self) -> int:
        return self.end - self.start

    @property
    def utf16_units(self) -> int:
        return self.source.utf16_units(self.start, self.end)


class _Utf16TextState:
    """Small piece table used to track exact sequential OT positions.

    A diff produces at most ``MAX_DIFF_OPS`` operations, so scanning the
    resulting few thousand pieces is substantially cheaper than rescanning a
    multi-megabyte document for every operation while remaining exact.
    """

    def __init__(self, text: str) -> None:
        source = _TextSource.from_text(text)
        self._pieces = [_TextPiece(source, 0, len(text))] if text else []

    def utf16_offset(self, position: int) -> int:
        if position < 0:
            raise ValueError("OT position must be >= 0")

        remaining = position
        offset = 0
        for piece in self._pieces:
            piece_length = len(piece)
            if remaining <= piece_length:
                return offset + piece.source.utf16_units(
                    piece.start,
                    piece.start + remaining,
                )
            remaining -= piece_length
            offset += piece.utf16_units

        # Preserve the previous defensive behaviour for malformed positions
        # beyond the current document. Generated diffs should never need it.
        return offset + remaining

    def insert(self, position: int, text: str) -> None:
        if not text:
            return
        index = self._split_at(position)
        source = _TextSource.from_text(text)
        self._pieces.insert(index, _TextPiece(source, 0, len(text)))

    def delete(self, position: int, length: int) -> None:
        if length <= 0:
            return
        start = self._split_at(position)
        end = self._split_at(position + length)
        del self._pieces[start:end]

    def _split_at(self, position: int) -> int:
        if position < 0:
            raise ValueError("OT position must be >= 0")

        remaining = position
        for index, piece in enumerate(self._pieces):
            piece_length = len(piece)
            if remaining == 0:
                return index
            if remaining < piece_length:
                split = piece.start + remaining
                left = _TextPiece(piece.source, piece.start, split)
                right = _TextPiece(piece.source, split, piece.end)
                self._pieces[index:index + 1] = [left, right]
                return index + 1
            remaining -= piece_length
        return len(self._pieces)


def convert_ot_positions_to_utf16(operations: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Convert all ``p`` fields from code-point to UTF-16 code-unit offsets.

    Operations are applied **sequentially** in sharejs-text-ot: each
    operation's position is relative to the document state after all
    previous operations.  This function correctly handles sequential
    positions by simulating each operation's effect on the text.

    Uses an exact piece-table simulation so large diffs and inserted astral
    characters cannot make later positions drift or land inside a surrogate
    pair.
    """
    if not operations:
        return operations

    state = _Utf16TextState(text)
    for op in operations:
        p = op["p"]
        op["p"] = state.utf16_offset(p)
        if "d" in op:
            state.delete(p, len(op["d"]))
        elif "i" in op:
            state.insert(p, op["i"])
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

    1. Preserve the exact code-point representation supplied by the caller.
    2. Sort edits by original position (descending).
    3. Walk back-to-front through the list, applying each edit on the
       already-modified text and verifying uniqueness.
    4. Compute a single diff from original → modified → OT operations.

    Raises ``EditMatchError`` if any ``old`` has zero or multiple matches.
    """
    operations, _ = _compute_edit_operations(current, edits)
    return operations


def _compute_edit_operations(
    current: str,
    edits: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], str]:
    """Like :func:`compute_edit_operations` but also returns the target content.

    Returning the modified document alongside the operations lets callers check
    the resulting size and recover from lost acks without re-applying the diff
    a second time (the simulation would otherwise duplicate work already done
    here at O(ops * len(doc))).
    """
    if not edits:
        return [], current

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
    return compute_diff_operations(current, modified), modified


# ---------------------------------------------------------------------------
# edit retry idempotency check
# ---------------------------------------------------------------------------


def check_edits_already_applied(current: str, expected_content: str | None) -> bool:
    """Return whether a lost-ack retry produced the exact submitted content."""
    return expected_content is not None and current == expected_content
