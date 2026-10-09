"""Deterministic structure-aware parsing for knowledge documents."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import re
from typing import Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader


PARSER_NAME = "memory-ops-structure-parser"
PARSER_VERSION = "1.0.0"
LocatorKind = Literal[
    "plain_text_lines",
    "markdown_lines",
    "json_pointer",
    "pdf_page_lines",
    "docx_paragraphs",
]


@dataclass(frozen=True)
class SourceLocator:
    kind: LocatorKind
    path: str
    start_line: int
    end_line: int
    structure_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedChunk:
    ordinal: int
    text: str
    content_hash: str
    locator: SourceLocator


@dataclass(frozen=True)
class ParsedDocument:
    chunks: tuple[ParsedChunk, ...]
    parser_name: str = PARSER_NAME
    parser_version: str = PARSER_VERSION


def parse_document(content: bytes, media_type: str, source_path: str) -> ParsedDocument:
    """Parse supported content into ordered chunks with exact source ranges."""

    if media_type == "application/pdf":
        chunks = _parse_pdf(content, source_path)
    elif media_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        chunks = _parse_docx(content, source_path)
    elif media_type == "application/json":
        decoded = content.decode("utf-8")
        chunks = _parse_json(decoded, source_path)
    elif media_type in {"text/plain", "text/markdown"}:
        decoded = content.decode("utf-8")
        chunks = _parse_lines(decoded, source_path, media_type == "text/markdown")
    else:
        raise ValueError(f"unsupported document media type: {media_type}")
    if not chunks:
        raise ValueError("document produced no parseable content")
    return ParsedDocument(tuple(chunks))


def _chunk(
    ordinal: int,
    text: str,
    kind: LocatorKind,
    path: str,
    start_line: int,
    end_line: int,
    structure_path: tuple[str, ...] = (),
) -> ParsedChunk:
    normalized = text.strip()
    return ParsedChunk(
        ordinal,
        normalized,
        sha256(normalized.encode("utf-8")).hexdigest(),
        SourceLocator(kind, path, start_line, end_line, structure_path),
    )


def _parse_lines(content: str, source_path: str, markdown: bool) -> list[ParsedChunk]:
    lines = content.splitlines()
    chunks: list[ParsedChunk] = []
    headings: list[str] = []
    block: list[str] = []
    block_start = 0
    fenced = False

    def flush(end_line: int) -> None:
        nonlocal block, block_start
        if not block:
            return
        text = "\n".join(block).strip()
        if text:
            kind: LocatorKind = "markdown_lines" if markdown else "plain_text_lines"
            chunks.append(
                _chunk(
                    len(chunks),
                    text,
                    kind,
                    _locator_path(source_path, headings if markdown else []),
                    block_start,
                    end_line,
                    tuple(headings),
                )
            )
        block = []
        block_start = 0

    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if markdown and stripped.startswith("```"):
            if not block:
                block_start = line_number
            block.append(line)
            fenced = not fenced
            continue
        heading = (
            None
            if fenced or not markdown
            else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        )
        if heading:
            flush(line_number - 1)
            level = len(heading.group(1))
            headings = headings[: level - 1]
            headings.append(heading.group(2))
            continue
        if not stripped and not fenced:
            flush(line_number - 1)
            continue
        if not block:
            block_start = line_number
        block.append(line)
    flush(len(lines))
    return chunks


def _parse_json(content: str, source_path: str) -> list[ParsedChunk]:
    value = json.loads(content)
    leaves = list(_json_leaves(value))
    chunks: list[ParsedChunk] = []
    cursor = 0
    line_count = max(1, len(content.splitlines()))
    for pointer, leaf in leaves:
        rendered = json.dumps(leaf, ensure_ascii=False, sort_keys=True)
        offset = content.find(rendered, cursor)
        if offset < 0:
            offset = content.find(rendered)
        if offset < 0:
            start_line, end_line = 1, line_count
        else:
            start_line = content.count("\n", 0, offset) + 1
            end_line = start_line + rendered.count("\n")
            cursor = offset + len(rendered)
        chunks.append(
            _chunk(
                len(chunks),
                rendered,
                "json_pointer",
                f"{source_path}#{pointer}",
                start_line,
                end_line,
                tuple(part for part in pointer.split("/") if part),
            )
        )
    return chunks


def _parse_pdf(content: bytes, source_path: str) -> list[ParsedChunk]:
    try:
        reader = PdfReader(BytesIO(content))
        chunks: list[ParsedChunk] = []
        for page_number, page in enumerate(reader.pages, 1):
            page_path = f"{source_path}#page={page_number}"
            for parsed in _parse_lines(page.extract_text() or "", page_path, False):
                chunks.append(
                    _chunk(
                        len(chunks),
                        parsed.text,
                        "pdf_page_lines",
                        page_path,
                        parsed.locator.start_line,
                        parsed.locator.end_line,
                    )
                )
        return chunks
    except Exception as error:
        raise ValueError("invalid or unreadable PDF document") from error


def _parse_docx(content: bytes, source_path: str) -> list[ParsedChunk]:
    word_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    try:
        with ZipFile(BytesIO(content)) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
    except (BadZipFile, KeyError, ElementTree.ParseError) as error:
        raise ValueError("invalid or unreadable DOCX document") from error

    chunks: list[ParsedChunk] = []
    headings: list[str] = []
    paragraph_tag = f"{{{word_namespace}}}p"
    text_tag = f"{{{word_namespace}}}t"
    style_tag = f"{{{word_namespace}}}pStyle"
    style_value = f"{{{word_namespace}}}val"
    for paragraph_number, paragraph in enumerate(document.iter(paragraph_tag), 1):
        paragraph_text = "".join(
            node.text or "" for node in paragraph.iter(text_tag)
        ).strip()
        if not paragraph_text:
            continue
        style = paragraph.find(f".//{style_tag}")
        heading = re.fullmatch(
            r"Heading([1-6])",
            style.get(style_value, "") if style is not None else "",
            re.IGNORECASE,
        )
        if heading:
            level = int(heading.group(1))
            headings = headings[: level - 1]
            headings.append(paragraph_text)
            continue
        chunks.append(
            _chunk(
                len(chunks),
                paragraph_text,
                "docx_paragraphs",
                f"{source_path}#paragraph={paragraph_number}",
                paragraph_number,
                paragraph_number,
                tuple(headings),
            )
        )
    return chunks


def _json_leaves(value: object, pointer: str = "") -> list[tuple[str, object]]:
    if isinstance(value, dict):
        if not value:
            return [(pointer or "/", value)]
        leaves: list[tuple[str, object]] = []
        for key, child in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            leaves.extend(_json_leaves(child, f"{pointer}/{escaped}"))
        return leaves
    if isinstance(value, list):
        if not value:
            return [(pointer or "/", value)]
        leaves = []
        for index, child in enumerate(value):
            leaves.extend(_json_leaves(child, f"{pointer}/{index}"))
        return leaves
    return [(pointer or "/", value)]


def _locator_path(source_path: str, headings: list[str]) -> str:
    if not headings:
        return source_path
    slug = re.sub(r"[^a-z0-9]+", "-", headings[-1].lower()).strip("-")
    return f"{source_path}#{slug}" if slug else source_path
