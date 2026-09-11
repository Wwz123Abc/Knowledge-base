import pymupdf
import pytest
from PIL import Image

from app.config import Settings
from app.core.loaders import load_document


def test_load_markdown(tmp_path):
    path = tmp_path / "policy.md"
    path.write_text("# 制度\n\n员工应遵守制度。", encoding="utf-8")

    pages = load_document(path)

    assert len(pages) == 1
    assert "员工应遵守制度" in pages[0].content


def test_empty_text_returns_no_pages(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("  ", encoding="utf-8")

    assert load_document(path) == []


def test_load_searchable_pdf_with_page_number(tmp_path):
    path = tmp_path / "policy.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Travel expenses require an invoice.")
    document.save(path)
    document.close()

    pages = load_document(path)

    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert "invoice" in pages[0].content


def test_image_requires_ocr_to_be_enabled(tmp_path):
    path = tmp_path / "notice.png"
    Image.new("RGB", (20, 20), color="white").save(path)

    with pytest.raises(ValueError, match="OCR_ENABLED"):
        load_document(path, Settings(ocr_enabled=False))
