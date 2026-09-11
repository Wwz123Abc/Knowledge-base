from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.loaders import LoadedPage

TABLE_PREFIX = "|"


def split_pages(
    pages: list[LoadedPage],
    base_metadata: dict[str, str | int | None],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n# ", "\n## ", "\n### ", "\n\n", "。", "；", "\n", " "],
        length_function=len,
    )
    documents: list[Document] = []
    for page in pages:
        metadata = {
            **base_metadata,
            "page_number": page.page_number,
            "section": page.section,
        }
        for section_content, section in _section_blocks(page.content, page.section):
            section_metadata = {**metadata, "section": section}
            for block, block_type in _semantic_blocks(section_content):
                if block_type == "table":
                    for table_chunk in _split_markdown_table(block, chunk_size):
                        table_metadata = {**section_metadata, "content_type": "table"}
                        documents.append(
                            Document(page_content=table_chunk, metadata=table_metadata)
                        )
                else:
                    documents.extend(
                        splitter.create_documents([block], metadatas=[section_metadata])
                    )
    for index, document in enumerate(documents):
        document.metadata["position"] = index
    return documents


def _section_blocks(content: str, default_section: str | None) -> list[tuple[str, str | None]]:
    blocks: list[tuple[str, str | None]] = []
    current_section = default_section
    current: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and stripped.lstrip("#").startswith(" "):
            block = "\n".join(current).strip()
            if block:
                blocks.append((block, current_section))
            current_section = stripped.lstrip("#").strip() or current_section
            current = [line]
        else:
            current.append(line)
    block = "\n".join(current).strip()
    if block:
        blocks.append((block, current_section))
    return blocks


def _semantic_blocks(content: str) -> list[tuple[str, str]]:
    """Separate Markdown tables so ordinary text splitting cannot cut through rows."""
    blocks: list[tuple[str, str]] = []
    current: list[str] = []
    current_type = "text"
    for line in content.splitlines():
        line_type = "table" if line.strip().startswith(TABLE_PREFIX) else "text"
        if current and line_type != current_type:
            block = "\n".join(current).strip()
            if block:
                blocks.append((block, current_type))
            current = []
        current.append(line)
        current_type = line_type
    block = "\n".join(current).strip()
    if block:
        blocks.append((block, current_type))
    return blocks


def _split_markdown_table(table: str, chunk_size: int) -> list[str]:
    lines = [line for line in table.splitlines() if line.strip()]
    if len(lines) <= 2 or len(table) <= chunk_size:
        return [table]
    header = lines[:2]
    rows = lines[2:]
    chunks: list[str] = []
    current = header.copy()
    for row in rows:
        if len(row) > chunk_size:
            # A single over-long row (e.g. a long paragraph inside one merged cell)
            # must be split at sentence boundaries, otherwise it stays one giant chunk.
            if len(current) > 2:
                chunks.append("\n".join(current))
                current = header.copy()
            for segment in _split_long_table_row(row, chunk_size):
                chunks.append("\n".join([*header, segment]))
            current = header.copy()
            continue
        candidate = "\n".join([*current, row])
        if len(candidate) > chunk_size and len(current) > 2:
            chunks.append("\n".join(current))
            current = [*header, row]
        else:
            current.append(row)
    if len(current) > 2:
        chunks.append("\n".join(current))
    return chunks or [table]


def _split_long_table_row(row: str, chunk_size: int) -> list[str]:
    """Split one over-long markdown row at sentence boundaries, keeping it searchable."""
    text = row.strip().strip("|").strip()
    limit = max(chunk_size - 8, 64)
    segments: list[str] = []
    buffer = ""
    for sentence in _sentence_parts(text):
        if buffer and len(buffer) + len(sentence) > limit:
            segments.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        segments.append(buffer)
    return [f"| {segment} |" for segment in segments if segment.strip()]


def _sentence_parts(text: str) -> list[str]:
    """Split text at Chinese/ASCII sentence boundaries while keeping the delimiter."""
    parts: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in "。；，、.!?；\n" and len(current) >= 16:
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    return parts or [text]
