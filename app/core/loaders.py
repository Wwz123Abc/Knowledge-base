from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pdfplumber
import pymupdf
import pytesseract
from docx import Document as DocxDocument
from PIL import Image
from pptx import Presentation

from app.config import Settings, get_settings

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".md", ".txt"}


@dataclass(slots=True)
class LoadedPage:
    content: str
    page_number: int | None = None
    section: str | None = None


def load_document(path: Path, settings: Settings | None = None) -> list[LoadedPage]:
    settings = settings or get_settings()
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"暂不支持 {suffix} 文件")
    if suffix == ".pdf":
        return _load_pdf(path, settings)
    if suffix == ".docx":
        return _load_docx(path)
    if suffix == ".pptx":
        return _load_pptx(path, settings)
    if suffix in {".png", ".jpg", ".jpeg"}:
        return _load_image(path, settings)
    return _load_text(path)


def _load_pdf(path: Path, settings: Settings) -> list[LoadedPage]:
    pages: list[LoadedPage] = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages):
            parts: list[str] = []
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(text)
            for table in page.extract_tables() or []:
                rendered = _table_to_markdown(table)
                if rendered:
                    parts.append(rendered)
            if not parts and settings.ocr_enabled:
                ocr_text = _ocr_pdf_page(path, index, settings.ocr_language)
                if ocr_text:
                    parts.append(ocr_text)
            content = "\n\n".join(parts).strip()
            if content:
                pages.append(LoadedPage(content=content, page_number=index + 1))
    return pages


def _ocr_pdf_page(path: Path, page_index: int, language: str) -> str:
    try:
        with pymupdf.open(path) as document:
            pixmap = document[page_index].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            image = Image.open(BytesIO(pixmap.tobytes("png")))
            return pytesseract.image_to_string(image, lang=language).strip()
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError("已启用 OCR，但系统未安装 Tesseract OCR") from exc


def _load_docx(path: Path) -> list[LoadedPage]:
    document = DocxDocument(str(path))
    pages: list[LoadedPage] = []
    current_section: str | None = None
    buffer: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        if paragraph.style and paragraph.style.name.startswith("Heading"):
            if buffer:
                pages.append(LoadedPage(content="\n".join(buffer), section=current_section))
                buffer = []
            current_section = text
        else:
            buffer.append(text)
    if buffer:
        pages.append(LoadedPage(content="\n".join(buffer), section=current_section))
    for table in document.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        rendered = _table_to_markdown(rows)
        if rendered:
            pages.append(LoadedPage(content=rendered, section="表格"))
    return pages


def _table_to_markdown(rows: list[list[str | None]]) -> str:
    cleaned = [[(cell or "").replace("\n", " ").strip() for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    # DOCX merged cells are expanded by python-docx into identical repeated cells.
    # Collapse them to a single cell so the same text is not duplicated N times.
    cleaned = [
        [row[0]] if len(row) > 1 and row[0] and len(set(row)) == 1 else row for row in cleaned
    ]
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    normalized = [row + [""] * (width - len(row)) for row in cleaned]
    header = normalized[0]
    separator = ["---"] * width
    body = normalized[1:]
    return "\n".join("| " + " | ".join(row) + " |" for row in [header, separator, *body])


def _load_pptx(path: Path, settings: Settings) -> list[LoadedPage]:
    presentation = Presentation(path)
    pages: list[LoadedPage] = []
    for index, slide in enumerate(presentation.slides, start=1):
        parts: list[str] = []
        title = None
        if slide.shapes.title and slide.shapes.title.text.strip():
            title = slide.shapes.title.text.strip()
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text.strip()
                if text and text != title:
                    parts.append(text)
            if getattr(shape, "has_table", False):
                rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                rendered = _table_to_markdown(rows)
                if rendered:
                    parts.append(rendered)
            if getattr(shape, "has_chart", False):
                rendered = _chart_to_text(shape.chart)
                if rendered:
                    parts.append(rendered)
            if settings.ocr_enabled and getattr(shape, "shape_type", None) == 13:
                try:
                    image = Image.open(BytesIO(shape.image.blob))
                    text = pytesseract.image_to_string(image, lang=settings.ocr_language).strip()
                    if text:
                        parts.append(f"图片文字：{text}")
                except (OSError, pytesseract.TesseractError):
                    pass
        content = "\n\n".join(parts).strip()
        if title:
            content = f"# {title}\n\n{content}".strip()
        if content:
            pages.append(LoadedPage(content=content, page_number=index, section=title))
    return pages


def _chart_to_text(chart) -> str:
    lines = ["图表数据："]
    for series in chart.series:
        name = str(series.name or "系列")
        values = [str(value) for value in series.values]
        lines.append(f"- {name}: {', '.join(values)}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _load_image(path: Path, settings: Settings) -> list[LoadedPage]:
    if not settings.ocr_enabled:
        raise ValueError("图片入库需要启用 OCR_ENABLED")
    try:
        with Image.open(path) as image:
            content = pytesseract.image_to_string(image, lang=settings.ocr_language).strip()
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError("已启用 OCR，但系统未安装 Tesseract OCR") from exc
    return [LoadedPage(content=content, section="图片 OCR")] if content else []


def _load_text(path: Path) -> list[LoadedPage]:
    content = path.read_text(encoding="utf-8-sig").strip()
    return [LoadedPage(content=content)] if content else []
