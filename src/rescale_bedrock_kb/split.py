"""Split the source user guide into per-chapter PDFs.

The corpus is a single 240 MB / 14,125-page PDF, which is well past what a
Bedrock KB S3 data source will ingest as one object. Splitting on the PDF's own
outline keeps each part topically coherent (so chunks don't straddle unrelated
chapters) and records the source page range in the filename, which lets an eval
result be traced back to a page in the original guide.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from . import config

MANIFEST_NAME = config.MANIFEST_NAME


@dataclass
class Part:
    """One output PDF, with provenance back to the source page range."""

    filename: str
    chapter: str
    part_index: int
    part_count: int
    start_page: int  # 0-based, inclusive, in the source PDF
    end_page: int  # 0-based, inclusive
    pages: int

    @property
    def in_subset(self) -> bool:
        return _matches_subset(self.chapter)


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text) or "untitled"


def _matches_subset(chapter: str) -> bool:
    low = chapter.lower()
    return any(name.lower() in low for name in config.SUBSET_CHAPTERS)


def chapter_ranges(reader: PdfReader) -> list[tuple[str, int, int]]:
    """Top-level outline entries as (title, start_page, end_page) inclusive."""
    tops = []
    for item in reader.outline:
        if isinstance(item, list):  # nested children -- we only want top level
            continue
        try:
            page = reader.get_destination_page_number(item)
        except Exception:  # noqa: BLE001 - a broken destination shouldn't abort the split
            continue
        tops.append((str(item.title).strip(), page))

    if not tops:
        raise SystemExit("Source PDF has no usable outline; cannot split on chapters.")

    total = len(reader.pages)
    ranges = []
    for i, (title, start) in enumerate(tops):
        end = tops[i + 1][1] - 1 if i + 1 < len(tops) else total - 1
        if end >= start:
            ranges.append((title, start, end))
    return ranges


def plan(reader: PdfReader) -> list[Part]:
    """Compute the output parts without writing anything.

    Chapters over MAX_PAGES_PER_PART are divided into equal-ish parts; the
    3,710-page Physics chapter would otherwise dwarf every other object.
    """
    parts: list[Part] = []
    for title, start, end in chapter_ranges(reader):
        span = end - start + 1
        count = max(1, -(-span // config.MAX_PAGES_PER_PART))  # ceil division
        per = -(-span // count)
        slug = _slug(title)
        for idx in range(count):
            p_start = start + idx * per
            p_end = min(end, p_start + per - 1)
            if p_start > p_end:
                continue
            suffix = f"-part{idx + 1:02d}" if count > 1 else ""
            # 1-based page numbers in the name: they match what a reader sees.
            name = f"{slug}{suffix}__p{p_start + 1:05d}-{p_end + 1:05d}.pdf"
            parts.append(
                Part(
                    filename=name,
                    chapter=title,
                    part_index=idx + 1,
                    part_count=count,
                    start_page=p_start,
                    end_page=p_end,
                    pages=p_end - p_start + 1,
                )
            )
    return parts


def write_parts(parts: list[Part], reader: PdfReader, out_dir: Path, overwrite: bool = False):
    """Write each part to `out_dir`, yielding (part, was_written)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for part in parts:
        target = out_dir / part.filename
        if target.exists() and not overwrite:
            yield part, False
            continue
        writer = PdfWriter()
        for page_no in range(part.start_page, part.end_page + 1):
            writer.add_page(reader.pages[page_no])
        with target.open("wb") as fh:
            writer.write(fh)
        writer.close()
        yield part, True


def save_manifest(parts: list[Part], out_dir: Path) -> Path:
    path = out_dir / MANIFEST_NAME
    payload = {
        "source_pdf": config.SOURCE_PDF.name,
        "source_pages": sum(p.pages for p in parts),
        "max_pages_per_part": config.MAX_PAGES_PER_PART,
        "subset_chapters": list(config.SUBSET_CHAPTERS),
        "parts": [asdict(p) for p in parts],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def load_manifest(out_dir: Path | None = None) -> list[Part]:
    out_dir = out_dir or config.SPLIT_DIR
    path = out_dir / MANIFEST_NAME
    if not path.exists():
        raise SystemExit(f"No split manifest at {path}. Run `split` first.")
    data = json.loads(path.read_text())
    return [Part(**p) for p in data["parts"]]


def select(parts: list[Part], subset: bool) -> list[Part]:
    if not subset:
        return parts
    chosen = [p for p in parts if p.in_subset]
    if not chosen:
        raise SystemExit(
            "Subset selection matched no chapters; check config.SUBSET_CHAPTERS."
        )
    return chosen
