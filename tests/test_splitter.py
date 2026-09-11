from app.core.loaders import LoadedPage
from app.core.splitter import split_pages


def test_split_pages_keeps_enterprise_metadata():
    pages = [LoadedPage(content="第一条制度。" * 20, page_number=3, section="报销制度")]
    chunks = split_pages(
        pages,
        base_metadata={
            "document_id": "doc-1",
            "title": "员工手册",
            "tenant_id": "tenant-a",
            "access_group": "finance",
        },
        chunk_size=40,
        chunk_overlap=5,
    )

    assert len(chunks) > 1
    assert chunks[0].metadata["document_id"] == "doc-1"
    assert chunks[0].metadata["page_number"] == 3
    assert chunks[0].metadata["section"] == "报销制度"
    assert [item.metadata["position"] for item in chunks] == list(range(len(chunks)))


def test_split_pages_keeps_table_rows_and_repeats_header():
    table = "\n".join(
        [
            "| 故障代码 | 原因 | 处理方法 |",
            "| --- | --- | --- |",
            *[
                f"| E{index:02d} | 传感器异常{index} | 检查接线并复位{index} |"
                for index in range(12)
            ],
        ]
    )

    chunks = split_pages(
        [LoadedPage(content=table, section="故障表")],
        {"document_id": "doc-table", "tenant_id": "tenant-a"},
        chunk_size=130,
        chunk_overlap=10,
    )

    assert len(chunks) > 1
    assert all(chunk.metadata["content_type"] == "table" for chunk in chunks)
    assert all(chunk.page_content.startswith("| 故障代码 | 原因 | 处理方法 |") for chunk in chunks)
    combined = "\n".join(chunk.page_content for chunk in chunks)
    assert all(f"| E{index:02d} |" in combined for index in range(12))


def test_split_pages_tracks_markdown_heading_as_section():
    chunks = split_pages(
        [LoadedPage(content="# 安装步骤\n断电后接线。\n## 故障处理\n检查传感器。")],
        {"document_id": "doc-heading", "tenant_id": "tenant-a"},
        chunk_size=80,
        chunk_overlap=5,
    )

    assert [chunk.metadata["section"] for chunk in chunks] == ["安装步骤", "故障处理"]


def test_split_pages_splits_over_long_single_table_row():
    long_text = (
        "工作原理：本设备用于测试产品。"
        + "真空吸盘吸住产品。" * 60
        + "测试数据保存在中央控制计算机上。"
    )
    table = "\n".join(["| 项目 | 说明 |", "| --- | --- |", f"| 工作原理 | {long_text} |"])

    chunks = split_pages(
        [LoadedPage(content=table, section="表格")],
        {"document_id": "doc-long", "tenant_id": "tenant-a"},
        chunk_size=200,
        chunk_overlap=10,
    )

    assert len(chunks) > 1
    assert all(chunk.metadata["content_type"] == "table" for chunk in chunks)
    assert all(len(chunk.page_content) <= 220 for chunk in chunks)
    assert all(chunk.page_content.startswith("| 项目 |") for chunk in chunks)
    combined = "\n".join(chunk.page_content for chunk in chunks)
    assert "真空吸盘吸住产品。" in combined
    assert "中央控制计算机" in combined


def test_table_to_markdown_collapses_merged_duplicate_cells():
    from app.core.loaders import _table_to_markdown

    # DOCX merged cells: python-docx repeats the merged text in every column cell.
    merged_rows = [
        ["品名"] * 4,
        ["长文本内容" * 20] * 4,
    ]
    rendered = _table_to_markdown(merged_rows)
    lines = rendered.splitlines()

    assert len(lines) == 3
    assert lines[0].count("|") == 2  # one data cell + fences
    assert "长文本内容" * 20 in lines[2]

    # Ordinary tables with distinct cells must keep their columns.
    normal_rows = [["品名", "说明"], ["治具", "功能用途"]]
    normal = _table_to_markdown(normal_rows)
    assert normal.splitlines()[0].count("|") == 3  # two columns -> three pipes
    assert "治具" in normal
