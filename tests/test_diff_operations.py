from __future__ import annotations

import random
import string
from typing import Any

import pytest

from sharelatex_mcp.projects import _compute_diff_operations


def _apply_ops(text: str, ops: list[dict[str, Any]]) -> str:
    """Simulate sequential OT application of a batch of operations."""
    for op in ops:
        p = op["p"]
        if "d" in op:
            d = op["d"]
            text = text[:p] + text[p + len(d):]
        elif "i" in op:
            text = text[:p] + op["i"] + text[p:]
        else:
            raise ValueError(f"Malformed operation (missing 'd' or 'i'): {op!r}")
    return text


# ── Roundtrip invariant: applying ops to old must produce new ──────────

@pytest.mark.parametrize("old, new", [
    # 基本编辑
    ("abc", "abc"),
    ("a", "ab"),
    ("ab", "a"),
    ("ab", "aXb"),
    ("aXb", "ab"),
    ("aXb", "aYb"),
    ("ab", "cd"),
    ("", "abc"),
    ("abc", ""),
    # 边界位置 — 开头操作
    ("Xabc", "abc"),
    ("abc", "Xabc"),
    ("abc", "Xac"),
    # 多处修改 — 验证位置偏移
    ("aXbYc", "aXAAbBBYc"),
    ("aXAAbBBYc", "aXbYc"),
    ("abXdeYfg", "abPQdeRSfg"),
    ("hello world", "hello there world"),
    ("hello there world", "hello world"),
    ("hello X world", "hello Y world"),
    # Unicode（双向）
    ("\u4f60\u597d\u4e16\u754c", "\u4f60\u597d\u65b0\u4e16\u754c"),
    ("\u4f60\u597d\u65b0\u4e16\u754c", "\u4f60\u597d\u4e16\u754c"),
    ("caf\u00e9", "caf\u00e9 au lait"),
    # 空白字符
    ("a\tb", "a\t\tb"),
    ("line1\r\nline2", "line1\r\nline3"),
    ("a  b", "a\tb"),
    # LaTeX 真实场景
    ("\\begin{document}\nHello\n\\end{document}", "\\begin{document}\nWorld\n\\end{document}"),
    # 大段匹配 + 小改动
    ("a" * 1000, "a" * 1000),
    ("a" * 500 + "X" + "b" * 500, "a" * 500 + "Y" + "b" * 500),
])
def test_roundtrip_apply_ops_matches_new(old: str, new: str) -> None:
    """Applying generated ops to old text must produce new text."""
    ops = _compute_diff_operations(old, new)
    assert _apply_ops(old, ops) == new


def test_roundtrip_randomized() -> None:
    """Fuzz test: random edits on random strings must roundtrip correctly."""
    rng = random.Random(42)
    ascii_alphabet = string.ascii_letters + string.digits + " \n\t"
    unicode_alphabet = ascii_alphabet + "\u4f60\u597d\u4e16\u754c\u00e9\u00e0\u00fc"

    for phase, alphabet in [("ascii", ascii_alphabet), ("unicode", unicode_alphabet)]:
        iterations = 200 if phase == "ascii" else 100
        for i in range(iterations):
            size = rng.randint(10, 200)
            old = "".join(rng.choice(alphabet) for _ in range(size))
            chars = list(old)
            for _ in range(rng.randint(1, 5)):
                pos = rng.randint(0, len(chars))
                edit_type = rng.choice(["insert", "delete", "replace"])
                if edit_type == "insert":
                    insert_len = rng.randint(1, 10)
                    chars[pos:pos] = [rng.choice(alphabet) for _ in range(insert_len)]
                elif edit_type == "delete" and pos < len(chars):
                    delete_len = rng.randint(1, min(5, len(chars) - pos))
                    del chars[pos:pos + delete_len]
                elif edit_type == "replace" and pos < len(chars):
                    replace_len = rng.randint(1, min(3, len(chars) - pos))
                    chars[pos:pos + replace_len] = [rng.choice(alphabet) for _ in range(replace_len)]
            new = "".join(chars)
            ops = _compute_diff_operations(old, new)
            assert _apply_ops(old, ops) == new, (
                f"Roundtrip failed [{phase}#{i}]:\n  old={old!r}\n  new={new!r}\n  ops={ops}"
            )


# ── Fixed expected-value tests (only for non-ambiguous scenarios) ──────

@pytest.mark.parametrize("old, new, expected", [
    ("abc", "abc", []),
    ("a", "ab", [{"p": 1, "i": "b"}]),
    ("ab", "a", [{"p": 1, "d": "b"}]),
    ("ab", "aXb", [{"p": 1, "i": "X"}]),
    ("aXb", "ab", [{"p": 1, "d": "X"}]),
    ("aXb", "aYb", [{"p": 1, "d": "X"}, {"p": 1, "i": "Y"}]),
    ("ab", "cd", [{"p": 0, "d": "ab"}, {"p": 0, "i": "cd"}]),
    ("", "abc", [{"p": 0, "i": "abc"}]),
    ("abc", "", [{"p": 0, "d": "abc"}]),
])
def test_fixed_expected_ops(old: str, new: str, expected: list[dict[str, Any]]) -> None:
    """Exact operation sequences for simple, non-ambiguous scenarios."""
    ops = _compute_diff_operations(old, new)
    assert ops == expected


# ── Large input tests ──────────────────────────────────────────────────

@pytest.mark.timeout(5)
def test_large_file_single_char_change() -> None:
    """500KB file with single character change must complete quickly."""
    old = "x" * 500_000
    new = old[:250_000] + "y" + old[250_001:]
    ops = _compute_diff_operations(old, new)
    assert _apply_ops(old, ops) == new


@pytest.mark.timeout(5)
def test_large_file_full_replacement_performance() -> None:
    """Diff on wholly different large inputs must complete and be correct."""
    old = "line " + "x" * 500_000
    new = "line " + "y" * 500_000
    ops = _compute_diff_operations(old, new)
    assert _apply_ops(old, ops) == new
