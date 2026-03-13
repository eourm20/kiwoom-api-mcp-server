from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server import FastMCP
from dotenv import load_dotenv

from kiwoom_mcp.catalog_index import (
    find_by_code,
    get_catalog_source_pdf,
    page_range_for_code,
    search_catalog,
)
from kiwoom_mcp.kiwoom_client import KiwoomRestClient
from kiwoom_mcp.pdf_spec_extractor import extract_api_spec_from_pdf

KST = ZoneInfo("Asia/Seoul")


def _load_env_files() -> None:
    module_dir = Path(__file__).resolve().parent
    # Priority: kiwoom_mcp/.env -> project-root/.env (fallback)
    load_dotenv(module_dir / ".env", override=False)
    load_dotenv(module_dir.parent / ".env", override=False)


_load_env_files()


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"Missing required env var: {name}")
    return str(value).strip()


mcp = FastMCP(
    "kiwoom-mcp",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8000")),
    streamable_http_path=os.getenv("MCP_HTTP_PATH", "/mcp"),
)


def _build_client() -> KiwoomRestClient:
    return KiwoomRestClient(
        base_url=_env("KIWOOM_BASE_URL"),
        app_key=_env("KIWOOM_APP_KEY"),
        app_secret=_env("KIWOOM_APP_SECRET"),
        account_no=_env("KIWOOM_ACCOUNT_NO"),
        token_path=os.getenv("KIWOOM_TOKEN_PATH", "/oauth2/token"),
        account_path=os.getenv("KIWOOM_ACCOUNT_PATH", "/api/dostk/acnt"),
        ws_base_url=os.getenv("KIWOOM_WS_BASE_URL", "wss://api.kiwoom.com:10000"),
        realtime_path=os.getenv("KIWOOM_REALTIME_PATH", "/api/dostk/websocket"),
        deposits_api_id=os.getenv("KIWOOM_DEPOSITS_API_ID", "kt00015"),
        trades_api_id=os.getenv("KIWOOM_TRADES_API_ID", "kt00015"),
        account_balance_api_id=os.getenv("KIWOOM_ACCOUNT_BALANCE_API_ID", "kt00001"),
        dmst_stex_tp=os.getenv("KIWOOM_DMST_STEX_TP", "%"),
        gds_tp=os.getenv("KIWOOM_GDS_TP", "1"),
        crnc_cd=os.getenv("KIWOOM_CRNC_CD", "KRW"),
    )


def _resolve_configured_path(configured: str) -> str:
    raw = configured.strip()
    if not raw:
        return ""
    path = Path(raw)
    if path.is_absolute():
        return str(path)

    module_dir = Path(__file__).resolve().parent
    candidates = [
        module_dir / raw,
        module_dir.parent / raw,
        Path.cwd() / raw,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return str((module_dir.parent / raw).resolve())


def _catalog_path() -> str:
    configured = os.getenv("KIWOOM_CATALOG_PATH", "").strip()
    if configured:
        return _resolve_configured_path(configured)
    module_dir = Path(__file__).resolve().parent
    docs_catalog = module_dir.parent / "docs" / "KIWOOM_REST_API_CATALOG.md"
    if docs_catalog.exists():
        return str(docs_catalog)
    return str(module_dir / "KIWOOM_REST_API_CATALOG.md")


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    return {
        "code": entry.code,
        "name": entry.name,
        "major": entry.major,
        "minor": entry.minor,
        "page": entry.page,
        "kind": entry.kind,
    }


def _default_pdf_path() -> str:
    configured = os.getenv("KIWOOM_API_PDF_PATH", "").strip()
    if configured:
        return _resolve_configured_path(configured)
    from_catalog = get_catalog_source_pdf(_catalog_path())
    if from_catalog:
        return from_catalog
    return ""


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _trade_execution_globally_allowed() -> bool:
    return _is_truthy(os.getenv("KIWOOM_ALLOW_TRADE_EXECUTION", "false"))


def _catalog_item_for_api(api_id: str) -> Any | None:
    return find_by_code(_catalog_path(), api_id)


def _is_trade_api(api_id: str) -> bool:
    entry = _catalog_item_for_api(api_id)
    if entry is None:
        return False
    minor = str(getattr(entry, "minor", "") or "")
    name = str(getattr(entry, "name", "") or "")
    trade_keywords = ("주문", "정정", "취소", "매수", "매도")
    trade_categories = ("주문", "신용주문")
    return minor in trade_categories or any(keyword in name for keyword in trade_keywords)


def _trade_approval_response(
    *,
    api_id: str,
    body: dict[str, Any] | None,
    path: str,
    approval_note: str,
) -> dict[str, Any]:
    entry = _catalog_item_for_api(api_id)
    return {
        "ok": False,
        "mode": "approval_required",
        "message": (
            "Trade execution is blocked by default. "
            "Set KIWOOM_ALLOW_TRADE_EXECUTION=true and call again with approve_trade=true."
        ),
        "api_id": api_id,
        "catalog_item": _entry_to_dict(entry) if entry is not None else None,
        "approval_requirements": {
            "env_var": "KIWOOM_ALLOW_TRADE_EXECUTION=true",
            "tool_argument": "approve_trade=true",
            "approval_note_required": True,
        },
        "request_preview": {
            "path": path,
            "body": body or {},
            "approval_note": approval_note,
        },
    }


def _build_auto_body(
    *,
    question: str,
    api_id: str,
    required_fields: list[str],
    overrides: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    body: dict[str, Any] = {}
    option_decisions: list[dict[str, Any]] = []
    q = question.strip()
    env_map = {
        "appkey": os.getenv("KIWOOM_APP_KEY", ""),
        "secretkey": os.getenv("KIWOOM_APP_SECRET", ""),
        "acctno": os.getenv("KIWOOM_ACCOUNT_NO", ""),
        "account_no": os.getenv("KIWOOM_ACCOUNT_NO", ""),
        "crnc_cd": os.getenv("KIWOOM_CRNC_CD", "KRW"),
        "gds_tp": os.getenv("KIWOOM_GDS_TP", "1"),
        "dmst_stex_tp": os.getenv("KIWOOM_DMST_STEX_TP", "%"),
        "frgn_stex_code": "",
    }
    lookback_decision = _infer_lookback_candidates_from_question(q)
    lookback_days = int(lookback_decision["selected"]["value"])
    today = datetime.now(tz=KST).date()
    strt_dt = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end_dt = today.strftime("%Y%m%d")
    option_decisions.append(lookback_decision)

    inferred: dict[str, Any] = {
        "strt_dt": strt_dt,
        "end_dt": end_dt,
        "stk_cd": "",
        "qry_tp": "2",
        "qry_dt": end_dt,
    }
    tp_decision: dict[str, Any] | None = None
    if api_id.lower() in ("kt00015", "ka10075", "ka10076", "ka10077"):
        tp_decision = _infer_tp_candidates_from_question(q)
        inferred["tp"] = tp_decision["selected"]["value"]
        option_decisions.append(tp_decision)

    # Guard against missing required fields from the PDF parser by merging inferred defaults.
    all_fields = list(required_fields)
    for fallback_key in inferred:
        if fallback_key not in [f.lower() for f in all_fields]:
            all_fields.append(fallback_key)

    for field in all_fields:
        key = field.lower()
        value = env_map.get(key, inferred.get(key, ""))
        if key == "tp" and not value:
            if tp_decision is None:
                tp_decision = _infer_tp_candidates_from_question(q)
                option_decisions.append(tp_decision)
            value = tp_decision["selected"]["value"]
        elif key == "qry_tp" and not value:
            value = "2"
            option_decisions.append(
                {
                    "field": "qry_tp",
                    "selected": {"value": "2", "reason": "default account query type"},
                    "alternatives": [
                        {"value": "1", "description": "alternative query type"},
                    ],
                }
            )
        elif key == "strt_dt" and not value:
            value = strt_dt
        elif key == "end_dt" and not value:
            value = end_dt
        elif key == "stk_cd" and not value:
            value = ""
            option_decisions.append(
                {
                    "field": "stk_cd",
                    "selected": {"value": "", "reason": "default to all symbols"},
                    "alternatives": [
                        {"value": "005930", "description": "single symbol example"},
                    ],
                }
            )
        body[field] = value if value != "" else "REQUIRED"
    if overrides:
        body.update(overrides)
    return body, option_decisions


def _option_selection_summary(option_decisions: list[dict[str, Any]]) -> list[str]:
    summary: list[str] = []
    for item in option_decisions:
        field = str(item.get("field", ""))
        selected = item.get("selected", {})
        if isinstance(selected, dict):
            selected_value = selected.get("value", "")
            reason = selected.get("reason", "")
        else:
            selected_value = selected
            reason = ""
        alternatives = item.get("alternatives", [])
        alt_values = []
        for alt in alternatives:
            if isinstance(alt, dict):
                alt_values.append(str(alt.get("value", "")))
            else:
                alt_values.append(str(alt))
        summary.append(
            f"{field}: selected={selected_value}"
            + (f" ({reason})" if reason else "")
            + (f" | alternatives={', '.join([x for x in alt_values if x != ''])}" if alt_values else "")
        )
    return summary


def _build_required_field_guidance(
    *,
    unresolved_fields: list[str],
    option_decisions: list[dict[str, Any]],
    use_realtime: bool,
) -> list[dict[str, Any]]:
    option_map: dict[str, dict[str, Any]] = {}
    for item in option_decisions:
        field = str(item.get("field", "")).strip()
        if field:
            option_map[field.lower()] = item

    guidance_defaults: dict[str, dict[str, Any]] = {
        "item": {
            "description": "Realtime target symbol code(s).",
            "example": "005930",
        },
        "stk_cd": {
            "description": "Stock symbol code.",
            "example": "005930",
        },
        "acctno": {
            "description": "Kiwoom account number.",
            "example": "12345678",
        },
        "account_no": {
            "description": "Kiwoom account number.",
            "example": "12345678",
        },
        "appkey": {
            "description": "Kiwoom app key.",
            "example": "from KIWOOM_APP_KEY",
        },
        "secretkey": {
            "description": "Kiwoom app secret.",
            "example": "from KIWOOM_APP_SECRET",
        },
        "tp": {
            "description": "Trade query type.",
            "example": "1",
        },
        "lookback_days": {
            "description": "How many days back to query.",
            "example": "7",
        },
    }

    guidance: list[dict[str, Any]] = []
    for field in sorted(set([x.strip().lower() for x in unresolved_fields if str(x).strip()])):
        base = guidance_defaults.get(
            field,
            {
                "description": "Required request field.",
                "example": "",
            },
        )
        option_info = option_map.get(field, {})
        selected = option_info.get("selected", {})
        selected_value = selected.get("value") if isinstance(selected, dict) else selected
        alternatives = option_info.get("alternatives", [])
        entry: dict[str, Any] = {
            "field": field,
            "required": True,
            "description": base["description"],
            "example": base["example"],
            "suggested_first_value": selected_value,
            "alternatives": alternatives,
        }
        if use_realtime and field == "item":
            entry["description"] = "Realtime target symbol code(s), required for websocket registration."
            entry["example"] = "005930"
        guidance.append(entry)
    return guidance


def _extract_symbol_candidates(question: str) -> list[str]:
    # KRX stock code is usually 6 digits (e.g. 005930)
    found = re.findall(r"\b(\d{6})\b", question)
    return sorted(set(found))


def _should_use_realtime(spec_result: dict[str, Any]) -> bool:
    catalog_item = spec_result.get("catalog_item", {}) if isinstance(spec_result, dict) else {}
    kind = str(catalog_item.get("kind", "")).strip().lower()
    if kind == "realtime":
        return True
    url = str(spec_result.get("url", "")).strip().lower()
    if url.startswith("wss://"):
        return True
    if "websocket" in url:
        return True
    return False


def _extract_lookback_days(question: str) -> int | None:
    q = question.replace(" ", "")
    m = re.search(r"(?:lookback_days|조회기간|기간)\s*[:=]\s*(\d+)", question, re.IGNORECASE)
    if m:
        return max(0, min(int(m.group(1)), 365))
    m = re.search(r"(\d+)일", q)
    if m:
        return max(1, min(int(m.group(1)), 365))
    m = re.search(r"(\d+)주", q)
    if m:
        return max(1, min(int(m.group(1)) * 7, 365))
    m = re.search(r"(\d+)개월", q)
    if m:
        return max(1, min(int(m.group(1)) * 30, 365))
    if "오늘" in q:
        return 0
    if "어제" in q:
        return 1
    return None


def _infer_lookback_candidates_from_question(question: str) -> dict[str, Any]:
    options = [
        {"value": 7, "description": "최근 1주"},
        {"value": 30, "description": "최근 1개월"},
        {"value": 90, "description": "최근 3개월"},
    ]
    selected = {"value": 7, "reason": "기본값: 첫 번째 옵션"}
    parsed_days = _extract_lookback_days(question)
    if parsed_days is not None:
        selected = {"value": parsed_days, "reason": "질문에 기간 값이 포함됨"}
    alternatives = [x for x in options if int(x["value"]) != int(selected["value"])]
    return {
        "field": "lookback_days",
        "selected": selected,
        "alternatives": alternatives,
    }


def _infer_tp_from_question(question: str) -> str:
    q = question.replace(" ", "")
    if "입금" in q:
        return "6"
    if "출금" in q:
        return "7"
    if "미체결" in q:
        return "1"
    if "매매" in q or "거래내역" in q or "거래" in q or "체결" in q:
        return "3"
    return "3"


def _infer_tp_candidates_from_question(question: str) -> dict[str, Any]:
    options = [
        {"value": "1", "description": "미체결 내역"},
        {"value": "3", "description": "매매/체결 내역"},
        {"value": "6", "description": "입금 내역"},
        {"value": "7", "description": "출금 내역"},
    ]
    q = question.replace(" ", "")
    selected = {"value": options[0]["value"], "reason": "기본값: 첫 번째 옵션"}
    explicit = re.search(r"\btp\s*[:=]\s*([1367])\b", question, re.IGNORECASE)
    if explicit:
        selected = {"value": explicit.group(1), "reason": "질문에 tp 값이 직접 포함됨"}
    elif "미체결" in q:
        selected = {"value": "1", "reason": "질문에 '미체결'이 포함됨"}
    elif "입금" in q:
        selected = {"value": "6", "reason": "질문에 '입금'이 포함됨"}
    elif "출금" in q:
        selected = {"value": "7", "reason": "질문에 '출금'이 포함됨"}
    elif "매매" in q or "거래내역" in q or "거래" in q or "체결" in q:
        selected = {"value": "3", "reason": "질문에 '거래/매매/체결'이 포함됨"}

    alternatives = [x for x in options if x["value"] != selected["value"]]
    return {
        "field": "tp",
        "selected": selected,
        "alternatives": alternatives,
    }


@mcp.tool()
def kiwoom_execute_api(
    api_id: str,
    body: dict[str, Any] | None = None,
    path: str = "",
    cont_yn: str = "N",
    next_key: str = "",
    max_pages: int = 1,
    approve_trade: bool = False,
    approval_note: str = "",
) -> dict[str, Any]:
    """
    Unified Kiwoom executor tool.
    Executes any Kiwoom API call with api_id + body, and returns raw payload pages.
    """
    if _is_trade_api(api_id):
        if not _trade_execution_globally_allowed() or not approve_trade or not approval_note.strip():
            return _trade_approval_response(
                api_id=api_id,
                body=body,
                path=path or os.getenv("KIWOOM_ACCOUNT_PATH", "/api/dostk/acnt"),
                approval_note=approval_note,
            )

    client = _build_client()
    try:
        result = client.execute_api(
            api_id=api_id,
            body=body,
            path=path or None,
            cont_yn=cont_yn,
            next_key=next_key,
            max_pages=max_pages,
        )
        return {"ok": True, **result}
    finally:
        client.close()


@mcp.tool()
def kiwoom_execute_realtime(
    api_id: str,
    item: str = "",
    items: list[str] | None = None,
    type_code: str = "",
    type_codes: list[str] | None = None,
    trnm: str = "REG",
    grp_no: str = "1",
    refresh: str = "1",
    timeout_seconds: int = 8,
    max_messages: int = 3,
) -> dict[str, Any]:
    """
    Execute realtime websocket request (e.g. 0B 주식체결).
    """
    selected_items = [x.strip() for x in (items or []) if str(x).strip()]
    if item.strip():
        selected_items.append(item.strip())
    selected_items = sorted(set(selected_items))
    if not selected_items:
        return {"ok": False, "message": "item or items is required"}

    selected_types = [x.strip() for x in (type_codes or []) if str(x).strip()]
    if type_code.strip():
        selected_types.append(type_code.strip())
    if not selected_types:
        # Common case: api_id itself is realtime type code (00, 0A, 0B ...)
        selected_types = [api_id.strip()]
    selected_types = sorted(set(selected_types))

    client = _build_client()
    try:
        result = client.execute_realtime(
            api_id=api_id,
            trnm=trnm,
            grp_no=grp_no,
            refresh=refresh,
            items=selected_items,
            types=selected_types,
            timeout_seconds=timeout_seconds,
            max_messages=max_messages,
        )
        return {"ok": True, **result}
    finally:
        client.close()


@mcp.tool()
def kiwoom_catalog_get(code: str) -> dict[str, Any]:
    """Get a single API/catalog item by code (e.g. ka10081, 0A, 공통)."""
    path = _catalog_path()
    item = find_by_code(path, code)
    if item is None:
        return {
            "ok": False,
            "message": "Code not found in catalog",
            "catalog_path": path,
            "code": code,
        }
    return {
        "ok": True,
        "catalog_path": path,
        "item": _entry_to_dict(item),
    }


@mcp.tool()
def kiwoom_catalog_search(
    query: str = "",
    kind: str = "all",
    major: str = "",
    minor: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """
    Search KIWOOM catalog by keyword/category.
    kind: all | rest | realtime | common
    """
    path = _catalog_path()
    items = search_catalog(
        catalog_path=path,
        query=query,
        kind=kind,
        major=major,
        minor=minor,
        limit=limit,
    )
    return {
        "ok": True,
        "catalog_path": path,
        "query": query,
        "kind": kind,
        "major": major,
        "minor": minor,
        "count": len(items),
        "items": [_entry_to_dict(x) for x in items],
    }


@mcp.tool()
def kiwoom_catalog_recommend_for_question(question: str, limit: int = 10) -> dict[str, Any]:
    """
    Recommend candidate APIs from a natural language question with keyword scoring.
    """
    q = question.strip()
    if not q:
        return {"ok": False, "message": "question is empty"}

    weighted_queries: list[tuple[str, int]] = []
    q_no_space = q.replace(" ", "")
    keyword_map = {
        "\uc794\uace0": "\uc794\uace0",
        "\uc608\uc218\uae08": "\uc608\uc218\uae08",
        "\uac70\ub798\ub0b4\uc5ed": "\uc704\ud0c1\uc885\ud569\uac70\ub798\ub0b4\uc5ed\uc694\uccad",
        "\ub9e4\ub9e4\ub0b4\uc5ed": "\uc704\ud0c1\uc885\ud569\uac70\ub798\ub0b4\uc5ed\uc694\uccad",
        "\uccb4\uacb0\ub0b4\uc5ed": "\uccb4\uacb0\uc694\uccad",
        "\uc2e4\ud604\uc190\uc775": "\uc77c\uc790\ubcc4\uc2e4\ud604\uc190\uc775\uc694\uccad",
        "\ub9e4\uc218": "\ub9e4\uc218\uc8fc\ubb38",
        "\ub9e4\ub3c4": "\ub9e4\ub3c4\uc8fc\ubb38",
        "\uc815\uc815": "\uc815\uc815\uc8fc\ubb38",
        "\ucde8\uc18c": "\ucde8\uc18c\uc8fc\ubb38",
        "\uc77c\ubd09": "\uc77c\ubd09\ucc28\ud2b8",
        "\uc8fc\ubd09": "\uc8fc\ubd09\ucc28\ud2b8",
        "\uc6d4\ubd09": "\uc6d4\ubd09\ucc28\ud2b8",
        "\ubd84\ubd09": "\ubd84\ubd09\ucc28\ud2b8",
        "\ud2f1": "\ud2f1\ucc28\ud2b8",
        "\uccb4\uacb0": "\uccb4\uacb0",
        "\ubbf8\uccb4\uacb0": "\ubbf8\uccb4\uacb0",
        "\uc5c5\uc885": "\uc5c5\uc885",
        "\uc21c\uc704": "\uc21c\uc704",
        "\uacf5\ub9e4\ub3c4": "\uacf5\ub9e4\ub3c4",
        "\uc870\uac74\uac80\uc0c9": "\uc870\uac74\uac80\uc0c9",
        "\uc2e4\uc2dc\uac04": "\uc2e4\uc2dc\uac04\uc2dc\uc138",
    }
    for token, mapped in keyword_map.items():
        if token in q or token in q_no_space:
            weighted_queries.append((mapped, 3))
    for token in q.replace("/", " ").replace(",", " ").split():
        if len(token) >= 2:
            weighted_queries.append((token, 1))
    weighted_queries.append((q, 1))

    scored: dict[str, tuple[int, Any]] = {}
    path = _catalog_path()
    for query, weight in weighted_queries:
        for item in search_catalog(catalog_path=path, query=query, limit=50):
            prev = scored.get(item.code)
            score = weight + (prev[0] if prev is not None else 0)
            scored[item.code] = (score, item)

    ranked = sorted(scored.values(), key=lambda x: (-x[0], x[1].page, x[1].code))
    top = [x[1] for x in ranked[: min(max(limit, 1), 30)]]
    return {
        "ok": True,
        "catalog_path": path,
        "question": question,
        "count": len(top),
        "items": [_entry_to_dict(x) for x in top],
    }


@mcp.tool()
def kiwoom_extract_api_spec(api_id: str, pdf_path: str = "") -> dict[str, Any]:
    """
    Extract API request spec from PDF using catalog page range.
    """
    catalog_path = _catalog_path()
    entry = find_by_code(catalog_path, api_id)
    if entry is None:
        return {"ok": False, "message": "api_id not found in catalog", "api_id": api_id}

    page_range = page_range_for_code(catalog_path, api_id)
    if page_range is None:
        return {"ok": False, "message": "failed to resolve page range", "api_id": api_id}

    source_pdf = (pdf_path or _default_pdf_path()).strip()
    if not source_pdf:
        return {
            "ok": False,
            "message": "PDF path is empty. Set KIWOOM_API_PDF_PATH or pass pdf_path.",
            "api_id": api_id,
            "page_range": {"start": page_range[0], "end": page_range[1]},
        }

    spec = extract_api_spec_from_pdf(
        pdf_path=source_pdf,
        api_id=api_id,
        page_start=page_range[0],
        page_end=page_range[1],
    )
    return {
        "ok": True,
        "catalog_item": _entry_to_dict(entry),
        "pdf_path": source_pdf,
        "page_range": {"start": spec.page_start, "end": spec.page_end},
        "method": spec.method,
        "url": spec.url,
        "required_headers": list(spec.request_required_headers),
        "required_body": list(spec.request_required_body),
        "raw_text_excerpt": spec.raw_text_excerpt,
    }


@mcp.tool()
def kiwoom_auto_call(
    question: str = "",
    api_id: str = "",
    body_overrides: dict[str, Any] | None = None,
    pdf_path: str = "",
    max_pages: int = 1,
    dry_run: bool = False,
    approve_trade: bool = False,
    approval_note: str = "",
) -> dict[str, Any]:
    """
    Auto pipeline:
    1) resolve api_id from question/catalog
    2) extract request spec from PDF page range
    3) auto-build body and execute API
    """
    chosen_api_id = api_id.strip()
    catalog_path = _catalog_path()

    if not chosen_api_id:
        q = question.strip()
        if not q:
            return {"ok": False, "message": "Either api_id or question is required"}
        rec = kiwoom_catalog_recommend_for_question(question=q, limit=10)
        candidates = rec.get("items", [])
        if not candidates:
            return {"ok": False, "message": "No API candidate found", "question": question}
        chosen_api_id = str(candidates[0]["code"])

    spec_result = kiwoom_extract_api_spec(api_id=chosen_api_id, pdf_path=pdf_path)
    if not spec_result.get("ok"):
        return spec_result

    required_body = [str(x) for x in spec_result.get("required_body", [])]
    request_body, option_decisions = _build_auto_body(
        question=question,
        api_id=chosen_api_id,
        required_fields=required_body,
        overrides=body_overrides,
    )
    unresolved = sorted([k for k, v in request_body.items() if v == "REQUIRED"])
    url = str(spec_result.get("url", "")).strip()
    use_realtime = _should_use_realtime(spec_result)

    realtime_items: list[str] = []
    realtime_types: list[str] = []
    if use_realtime:
        symbol_candidates = _extract_symbol_candidates(question)
        if symbol_candidates:
            realtime_items.extend(symbol_candidates)
        if body_overrides:
            override_item = str(body_overrides.get("item", "")).strip()
            if override_item:
                realtime_items.append(override_item)
            override_items = body_overrides.get("items", [])
            if isinstance(override_items, list):
                realtime_items.extend([str(x).strip() for x in override_items if str(x).strip()])
            override_type = str(body_overrides.get("type_code", "")).strip() or str(body_overrides.get("type", "")).strip()
            if override_type:
                realtime_types.append(override_type)
            override_types = body_overrides.get("type_codes", [])
            if isinstance(override_types, list):
                realtime_types.extend([str(x).strip() for x in override_types if str(x).strip()])
        if not realtime_types:
            realtime_types = [chosen_api_id]
        realtime_items = sorted(set(realtime_items))
        realtime_types = sorted(set(realtime_types))
        if not realtime_items:
            unresolved.append("item")

    if dry_run or unresolved:
        required_field_guidance = _build_required_field_guidance(
            unresolved_fields=unresolved,
            option_decisions=option_decisions,
            use_realtime=use_realtime,
        )
        return {
            "ok": True,
            "mode": "dry_run" if dry_run else "needs_input",
            "api_id": chosen_api_id,
            "question": question,
            "execution_mode": "realtime" if use_realtime else "rest",
            "resolved_spec": spec_result,
            "request_plan": {
                "path": (url or os.getenv("KIWOOM_REALTIME_PATH", "/api/dostk/websocket"))
                if use_realtime
                else (url or os.getenv("KIWOOM_ACCOUNT_PATH", "/api/dostk/acnt")),
                "body": (
                    {
                        "trnm": str((body_overrides or {}).get("trnm", "REG")),
                        "grp_no": str((body_overrides or {}).get("grp_no", "1")),
                        "refresh": str((body_overrides or {}).get("refresh", "1")),
                        "item": realtime_items,
                        "type": realtime_types,
                    }
                    if use_realtime
                    else request_body
                ),
                "unresolved_required_fields": unresolved,
                "required_field_guidance": required_field_guidance,
                "required_input_message": (
                    "Provide values for required fields before execution."
                    if unresolved
                    else ""
                ),
            },
            "auto_option_decisions": option_decisions,
            "option_selection_summary": _option_selection_summary(option_decisions),
        }

    if _is_trade_api(chosen_api_id):
        if not _trade_execution_globally_allowed() or not approve_trade or not approval_note.strip():
            return _trade_approval_response(
                api_id=chosen_api_id,
                body=request_body,
                path=url or os.getenv("KIWOOM_ACCOUNT_PATH", "/api/dostk/acnt"),
                approval_note=approval_note,
            )

    if use_realtime:
        execute_result = kiwoom_execute_realtime(
            api_id=chosen_api_id,
            items=realtime_items,
            type_codes=realtime_types,
            trnm=str((body_overrides or {}).get("trnm", "REG")),
            grp_no=str((body_overrides or {}).get("grp_no", "1")),
            refresh=str((body_overrides or {}).get("refresh", "1")),
            timeout_seconds=int((body_overrides or {}).get("timeout_seconds", 8)),
            max_messages=int((body_overrides or {}).get("max_messages", 3)),
        )
    else:
        execute_result = kiwoom_execute_api(
            api_id=chosen_api_id,
            body=request_body,
            path=url,
            max_pages=max_pages,
            approve_trade=approve_trade,
            approval_note=approval_note,
        )
    return {
        "ok": True,
        "api_id": chosen_api_id,
        "question": question,
        "execution_mode": "realtime" if use_realtime else "rest",
        "resolved_spec": spec_result,
        "request_body": (
            {
                "trnm": str((body_overrides or {}).get("trnm", "REG")),
                "grp_no": str((body_overrides or {}).get("grp_no", "1")),
                "refresh": str((body_overrides or {}).get("refresh", "1")),
                "item": realtime_items,
                "type": realtime_types,
            }
            if use_realtime
            else request_body
        ),
        "auto_option_decisions": option_decisions,
        "option_selection_summary": _option_selection_summary(option_decisions),
        "execute_result": execute_result,
    }


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
