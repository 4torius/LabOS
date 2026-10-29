"""Resolving a written instrument name to a discovered instrument.

A workflow file names its instruments as free text. Those files were authored
over months by different tools and people, so the same physical instrument is
written `Tecan`, `TecanM200Pro`, `tecan` and `Tecan M200 Pro` across the
Library, and the mobile robot appears as both `mobile` and
`GoFaGo Mobile Robot`.

Matching that text to a live instrument used to be done in five places with
five slightly different substring rules — including
`if instrument_lower in name_l or name_l in instrument_lower`, which happily
matches `manual` against `Manual Station` *and* against a future
`Manual Sample Prep`, and then silently takes whichever the dict happened to
yield first. On a machine that moves liquid, picking the wrong instrument
because of dictionary ordering is not an acceptable failure mode.

This module is the single implementation. It is deliberately layered from
strict to loose, and — the important part — **ambiguity is an error**: when a
name matches more than one instrument at the same confidence level, nothing is
returned and the caller is told which candidates collided.

It is a pure function over `(id, display_name)` pairs so both `LabCore` (which
holds `Instrument` objects) and `PnPRegistry` (which holds `PnPServer`s) can
use it without either importing the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, List, Optional, Sequence, Tuple, TypeVar

T = TypeVar("T")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_identifier(value: str) -> str:
    """Lowercase and strip everything that is not a letter or digit.

    This is what lets `OpentronsFlex`, `opentrons_flex` and `Opentrons Flex`
    all collapse to `opentronsflex` — the single most common cause of a
    workflow failing to find an instrument that is right there and online.
    """
    return _NON_ALNUM.sub("", (value or "").lower())


class MatchKind(str, Enum):
    EXACT_ID = "exact-id"
    EXACT_NAME = "exact-name"
    NORMALIZED = "normalized"
    SUBSTRING = "unique-substring"
    NOT_FOUND = "not-found"
    AMBIGUOUS = "ambiguous"
    EMPTY = "empty-name"

    @property
    def matched(self) -> bool:
        return self in (
            MatchKind.EXACT_ID, MatchKind.EXACT_NAME,
            MatchKind.NORMALIZED, MatchKind.SUBSTRING,
        )


@dataclass
class Resolution(Generic[T]):
    """Outcome of resolving one written name."""

    target: Optional[T] = None
    kind: MatchKind = MatchKind.NOT_FOUND
    #: ids that collided (AMBIGUOUS), or every known id (NOT_FOUND)
    candidates: List[str] = field(default_factory=list)
    query: str = ""

    def __bool__(self) -> bool:
        return self.target is not None

    @property
    def error(self) -> Optional[str]:
        """A message naming what went wrong and what the caller could use."""
        if self.target is not None:
            return None
        if self.kind is MatchKind.EMPTY:
            return "No instrument name given"
        available = ", ".join(self.candidates) or "none"
        if self.kind is MatchKind.AMBIGUOUS:
            return (
                f"Instrument '{self.query}' is ambiguous — it matches "
                f"{available}. Use the exact instrument id."
            )
        return f"Instrument '{self.query}' not found. Available: {available}"


def resolve(
    name: str,
    candidates: Sequence[Tuple[str, str, T]],
) -> Resolution[T]:
    """Resolve `name` against `(id, display_name, target)` triples.

    Layers, strictest first; the first layer that produces exactly one match
    wins, and a layer that produces several stops the search as ambiguous
    rather than falling through to a looser rule.
    """
    query = (name or "").strip()
    if not query:
        return Resolution(kind=MatchKind.EMPTY, query=query)

    all_ids = [cid for cid, _, _ in candidates]
    lowered = query.lower()
    normalized = normalize_identifier(query)

    def _pick(matches, kind) -> Optional[Resolution[T]]:
        if len(matches) == 1:
            cid, _, target = matches[0]
            return Resolution(target=target, kind=kind, candidates=[cid], query=query)
        if len(matches) > 1:
            return Resolution(
                kind=MatchKind.AMBIGUOUS,
                candidates=[cid for cid, _, _ in matches],
                query=query,
            )
        return None

    # NB: each layer is checked with `is not None`, never for truthiness — an
    # ambiguous Resolution is deliberately falsy (it carries no target), so a
    # truthiness check would fall through to the *looser* next layer and
    # resolve by luck exactly where the name was too vague to be trusted.

    # 1. Exact id — the canonical form, and what saved workflows are migrated to.
    hit = _pick([c for c in candidates if c[0].lower() == lowered], MatchKind.EXACT_ID)
    if hit is not None:
        return hit

    # 2. Exact display name, as announced by the server itself.
    hit = _pick([c for c in candidates if (c[1] or "").lower() == lowered], MatchKind.EXACT_NAME)
    if hit is not None:
        return hit

    # 3. Punctuation- and case-insensitive: "OpentronsFlex" == "Opentrons Flex".
    if normalized:
        matches = [
            c for c in candidates
            if normalize_identifier(c[0]) == normalized
            or normalize_identifier(c[1]) == normalized
        ]
        hit = _pick(matches, MatchKind.NORMALIZED)
        if hit is not None:
            return hit

    # 4. Containment, but only when exactly one instrument contains the query
    #    (or is contained by it). "Tecan" finds "TecanM200Pro"; "GoFaGo Mobile
    #    Robot" finds "Mobile Robot". Two matches here is a genuine ambiguity
    #    that the author has to resolve, not a coin flip for us to call.
    if normalized:
        matches = []
        for c in candidates:
            for field_value in (c[0], c[1]):
                target_norm = normalize_identifier(field_value)
                if not target_norm:
                    continue
                if normalized in target_norm or target_norm in normalized:
                    matches.append(c)
                    break
        hit = _pick(matches, MatchKind.SUBSTRING)
        if hit is not None:
            return hit

    return Resolution(kind=MatchKind.NOT_FOUND, candidates=all_ids, query=query)


__all__ = ["MatchKind", "Resolution", "normalize_identifier", "resolve"]
