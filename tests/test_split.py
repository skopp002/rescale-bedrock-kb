"""Tests for the PDF splitting plan.

These exercise the plan/selection logic against a stubbed reader so they run
without the 240 MB corpus present.
"""

from __future__ import annotations

import pytest

from rescale_bedrock_kb import config, split


class _FakeOutlineItem:
    def __init__(self, title: str):
        self.title = title


class _FakeReader:
    """Minimal stand-in for PdfReader: an outline plus a page count."""

    def __init__(self, entries: list[tuple[str, int]], total_pages: int):
        self._dests = {}
        self.outline = []
        for title, page in entries:
            item = _FakeOutlineItem(title)
            self._dests[id(item)] = page
            self.outline.append(item)
        self.pages = [object()] * total_pages

    def get_destination_page_number(self, item):
        return self._dests[id(item)]


def test_chapter_ranges_are_contiguous_and_inclusive():
    reader = _FakeReader([("Intro", 0), ("Middle", 10), ("End", 20)], 30)
    ranges = split.chapter_ranges(reader)
    assert ranges == [("Intro", 0, 9), ("Middle", 10, 19), ("End", 20, 29)]


def test_plan_splits_oversized_chapters(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGES_PER_PART", 100)
    reader = _FakeReader([("Huge", 0)], 250)
    parts = split.plan(reader)

    assert len(parts) == 3
    assert [p.pages for p in parts] == [84, 84, 82]
    # Parts must tile the chapter exactly, with no gaps or overlaps.
    assert parts[0].start_page == 0
    assert parts[-1].end_page == 249
    for earlier, later in zip(parts, parts[1:]):
        assert later.start_page == earlier.end_page + 1
    assert all(p.part_count == 3 for p in parts)


def test_plan_leaves_small_chapters_whole(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGES_PER_PART", 300)
    reader = _FakeReader([("Small", 0), ("Also Small", 50)], 100)
    parts = split.plan(reader)

    assert len(parts) == 2
    assert all(p.part_count == 1 for p in parts)
    # A single-part chapter carries no "-partNN" infix.
    assert "part" not in parts[0].filename


def test_filename_encodes_one_based_page_range(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGES_PER_PART", 300)
    reader = _FakeReader([("Getting Started", 99)], 189)
    (part,) = split.plan(reader)

    # 0-based 99..188 is 1-based 100..189 -- what a reader actually sees.
    assert part.filename == "getting-started__p00100-00189.pdf"
    assert (part.start_page, part.end_page) == (99, 188)


def test_select_subset_matches_configured_chapters(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGES_PER_PART", 300)
    monkeypatch.setattr(config, "SUBSET_CHAPTERS", ("Getting Started",))
    reader = _FakeReader([("Getting Started", 0), ("Physics Simulation", 50)], 100)
    parts = split.plan(reader)

    assert len(split.select(parts, subset=False)) == 2
    chosen = split.select(parts, subset=True)
    assert [p.chapter for p in chosen] == ["Getting Started"]


def test_select_subset_raises_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(config, "SUBSET_CHAPTERS", ("Nonexistent Chapter",))
    reader = _FakeReader([("Getting Started", 0)], 10)
    with pytest.raises(SystemExit):
        split.select(split.plan(reader), subset=True)


def test_plan_requires_an_outline():
    with pytest.raises(SystemExit):
        split.plan(_FakeReader([], 10))
