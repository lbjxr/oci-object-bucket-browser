from dataclasses import dataclass

from app.file_browser import FileBrowserQuery, classify_file_type, filter_and_paginate


@dataclass
class Folder:
    name: str


@dataclass
class File:
    name: str
    size: int | None
    content_type: str | None


def test_classify_file_type_uses_content_type_and_suffix():
    assert classify_file_type("image/png", "photo.bin") == "image"
    assert classify_file_type("application/octet-stream", "report.pdf") == "pdf"
    assert classify_file_type(None, "notes.md") == "text"
    assert classify_file_type("application/zip", "archive.data") == "archive"
    assert classify_file_type("application/octet-stream", "payload.bin") == "other"


def test_filter_and_paginate_applies_query_type_and_size():
    folders = [Folder("images"), Folder("reports")]
    files = [
        File("root/photo.png", 2 * 1024 * 1024, "image/png"),
        File("root/report.pdf", 6 * 1024 * 1024, "application/pdf"),
        File("root/readme.txt", 512, "text/plain"),
    ]

    result = filter_and_paginate(
        folders,
        files,
        FileBrowserQuery(query="report", file_type="pdf", size_min_mb=5, page=1, page_size=10),
    )

    assert result.folders == []
    assert [item.name for item in result.files] == ["root/report.pdf"]
    assert result.total_items == 1
    assert result.start_item == 1
    assert result.end_item == 1


def test_filter_and_paginate_keeps_folder_first_order_across_pages():
    folders = [Folder("alpha"), Folder("beta")]
    files = [File(f"root/file-{index}.txt", index, "text/plain") for index in range(10)]

    first_page = filter_and_paginate(folders, files, FileBrowserQuery(page=1, page_size=10))
    second_page = filter_and_paginate(folders, files, FileBrowserQuery(page=2, page_size=10))

    assert [item.name for item in first_page.folders] == ["alpha", "beta"]
    assert [item.name for item in first_page.files] == [f"root/file-{index}.txt" for index in range(8)]
    assert second_page.folders == []
    assert [item.name for item in second_page.files] == ["root/file-8.txt", "root/file-9.txt"]
    assert second_page.total_pages == 2
    assert second_page.has_previous is True
    assert second_page.has_next is False
