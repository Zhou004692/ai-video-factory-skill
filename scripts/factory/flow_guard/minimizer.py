"""Small deterministic delta minimizer for reproducible branch failures."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar


T = TypeVar("T")


def minimize_failure(
    items: Sequence[T],
    still_fails: Callable[[tuple[T, ...]], bool],
) -> tuple[T, ...]:
    """Remove inputs one by one while the same failure remains observable."""
    current = tuple(items)
    if not current or not still_fails(current):
        return current
    changed = True
    while changed and len(current) > 1:
        changed = False
        for index in range(len(current)):
            candidate = current[:index] + current[index + 1:]
            if candidate and still_fails(candidate):
                current = candidate
                changed = True
                break
    return current
