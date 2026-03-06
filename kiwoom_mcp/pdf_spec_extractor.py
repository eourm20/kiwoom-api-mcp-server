from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b", re.IGNORECASE)
URL_RE = re.compile(r"(/[\w\-/{}.?=&]+)")


@dataclass(frozen=True)
class ApiSpec:
    api_id: str
    page_start: int
    page_end: int
    method: str
    url: str
    request_required_body: tuple[str, ...]
    request_required_headers: tuple[str, ...]
    raw_text_excerpt: str


def _import_fitz() -> Any:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "PyMuPDF(fitz) is required. Install with: python -m pip install pymupdf"
        ) from exc
    return fitz


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _pick_label_value(lines: list[str], label: str) -> str:
    lowered = label.lower()
    for idx, line in enumerate(lines):
        if line.strip().lower() == lowered:
            for j in range(idx + 1, min(idx + 5, len(lines))):
                v = _clean(lines[j])
                if v:
                    return v
    return ""


def _extract_required_fields_from_text(text: str) -> tuple[list[str], list[str]]:
    required_body: list[str] = []
    required_headers: list[str] = []

    # Fallback text parse from table rows like:
    # Header api-id ... Y ...
    # Body appkey ... Y ...
    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            continue
        tokens = line.split(" ")
        if len(tokens) < 3:
            continue
        row_type = tokens[0].lower()
        element = tokens[1].strip().lower()
        if row_type not in ("header", "body"):
            continue
        if " y " not in f" {line.lower()} ":
            continue
        if row_type == "body":
            if element not in required_body:
                required_body.append(element)
        else:
            if element not in required_headers:
                required_headers.append(element)
    return required_body, required_headers


def extract_api_spec_from_pdf(
    *,
    pdf_path: str,
    api_id: str,
    page_start: int,
    page_end: int,
) -> ApiSpec:
    fitz = _import_fitz()
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(path))
    try:
        page_start = max(1, page_start)
        page_end = min(max(page_start, page_end), len(doc))
        texts: list[str] = []
        for page_no in range(page_start, page_end + 1):
            page = doc[page_no - 1]
            texts.append(page.get_text("text"))

        all_text = "\n".join(texts)
        lines = [x for x in (_clean(v) for v in all_text.splitlines()) if x]

        method_value = _pick_label_value(lines, "Method")
        method_match = METHOD_RE.search(method_value or all_text)
        method = method_match.group(1).upper() if method_match else "POST"

        url_value = _pick_label_value(lines, "URL")
        url_match = URL_RE.search(url_value or all_text)
        url = url_match.group(1) if url_match else ""

        required_body, required_headers = _extract_required_fields_from_text(all_text)

        excerpt = _clean(all_text)[:1200]
        return ApiSpec(
            api_id=api_id,
            page_start=page_start,
            page_end=page_end,
            method=method,
            url=url,
            request_required_body=tuple(required_body),
            request_required_headers=tuple(required_headers),
            raw_text_excerpt=excerpt,
        )
    finally:
        doc.close()
