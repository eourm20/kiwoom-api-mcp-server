from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
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

logger = logging.getLogger(__name__)

# quant_trading 루트를 sys.path에 추가
_module_dir = Path(__file__).resolve().parent
_quant_root = Path(os.getenv("QUANT_TRADING_PATH", "")).resolve() if os.getenv("QUANT_TRADING_PATH") else _module_dir.parent.parent
if str(_quant_root) not in sys.path:
    sys.path.insert(0, str(_quant_root))

# quant_trading .env 로드
load_dotenv(_quant_root / ".env", override=False)

# DB 초기화 (테이블 없으면 생성, 이미 있으면 무시)
try:
    from data.db import init_db
    init_db()
except Exception as e:
    logger.warning(f"[kiwoom-mcp] DB 초기화 실패 (quant 도구 사용 불가): {e}")


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


# ── Quant Trading Tools (직접 함수 호출 방식) ──────────────────────────────


def _is_trading_hours() -> bool:
    """평일 08:00~16:00 (프리장 포함)"""
    now = datetime.now()
    return now.weekday() < 5 and 8 <= now.hour < 16


def _auto_sync_loop(interval_seconds: int = 600):
    """Claude Desktop 실행 중 장 시간대 포트폴리오 자동 동기화 (백그라운드 스레드)."""
    time.sleep(30)
    while True:
        try:
            if _is_trading_hours():
                from worker.clients.kiwoom_client import KiwoomClient
                from worker.portfolio_sync import sync_all
                sync_all(KiwoomClient())
                logger.info("[kiwoom-mcp] 포트폴리오 자동 동기화 완료")
        except Exception as e:
            logger.warning(f"[kiwoom-mcp] 자동 동기화 실패: {e}")
        time.sleep(interval_seconds)


_sync_thread = threading.Thread(target=_auto_sync_loop, daemon=True, name="quant-auto-sync")
_sync_thread.start()


_CATEGORY_LABEL = {"trade": "매매 결정", "watchlist": "조건 변경", "general": "전략 메모"}
_ACTION_EMOJI = {
    "매매 결정": "💼", "조건 변경": "⚙️", "전략 메모": "📋",
    "전략 삭제": "🗑", "전략 추가": "📋",
}

_notify_buffer: list[tuple[str, str, str]] = []
_notify_timer: threading.Timer | None = None
_notify_lock = threading.Lock()
_NOTIFY_DELAY = 4.0


def _flush_notify_buffer() -> None:
    global _notify_buffer
    with _notify_lock:
        items = _notify_buffer[:]
        _notify_buffer = []
    if not items:
        return

    def _fmt(action: str, summary: str, ts: str, extra: str = "") -> str:
        emoji = _ACTION_EMOJI.get(action, "📌")
        escaped = _md_escape(summary)
        tail = f" (외 {extra}건)" if extra else ""
        return f"{emoji} *[{action}]*{tail}\n{escaped}"

    if len(items) == 1:
        action, summary, ts = items[0]
        _telegram_send(_fmt(action, summary, ts))
        return

    from collections import Counter
    action_counts: Counter = Counter(action for action, _, __ in items)
    action_first: dict[str, tuple[str, str]] = {}
    for action, summary, ts in items:
        if action not in action_first:
            action_first[action] = (summary, ts)

    lines = []
    for action, count in action_counts.items():
        summary, ts = action_first[action]
        extra = str(count - 1) if count > 1 else ""
        lines.append(_fmt(action, summary, ts, extra))
    _telegram_send("\n\n".join(lines))


_TG_MAX_LEN = 4000


def _md_escape(text: str) -> str:
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _telegram_send(text: str) -> bool:
    import httpx
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("[kiwoom-mcp] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정")
        return False

    if len(text) > _TG_MAX_LEN:
        text = text[:_TG_MAX_LEN] + "\n...(이하 생략)"

    def _post(parse_mode):
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10,
        )

    try:
        resp = _post("Markdown")
        if resp.status_code == 200:
            return True
        logger.warning(f"[kiwoom-mcp] Markdown 발송 실패({resp.status_code}), plain text 재시도")
        resp2 = _post(None)
        ok = resp2.status_code == 200
        if not ok:
            logger.warning(f"[kiwoom-mcp] plain text 발송도 실패: {resp2.status_code} {resp2.text[:200]}")
        return ok
    except Exception as e:
        logger.warning(f"[kiwoom-mcp] 텔레그램 발송 실패: {e}")
        return False


def _notify(action: str, summary: str, detail: str = "", ts: str = "") -> bool:
    global _notify_timer
    if not ts:
        ts = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    else:
        ts = ts[:19].replace("-", ".")
    with _notify_lock:
        _notify_buffer.append((action, summary, ts))
        if _notify_timer is not None:
            _notify_timer.cancel()
        _notify_timer = threading.Timer(_NOTIFY_DELAY, _flush_notify_buffer)
        _notify_timer.daemon = True
        _notify_timer.start()
    return True


def _log_and_notify(category: str, summary: str, detail: str = "") -> bool:
    created_at = ""
    display_summary = summary
    try:
        from data.db import save_strategy_note, get_strategy_notes
        save_strategy_note(category, summary, detail)
        notes = get_strategy_notes(limit=1)
        if notes:
            created_at = notes[0].get("created_at", "")
            note_id = notes[0].get("id", "")
            if note_id:
                display_summary = f"[ID:{note_id}] {summary}"
    except Exception as e:
        logger.warning(f"[kiwoom-mcp] strategy_note 저장 실패: {e}")

    label = _CATEGORY_LABEL.get(category, category)
    return _notify(label, display_summary, detail, ts=created_at)


def _get_report_fns():
    from worker.report import (
        report_signals, report_portfolio,
        report_trades, report_strategy, report_all,
    )
    return {
        "signals": report_signals,
        "portfolio": report_portfolio,
        "trades": report_trades,
        "strategy": report_strategy,
        "all": report_all,
    }


@mcp.tool()
def quant_report(type: str = "all", days: int = 1, limit: int = 20) -> dict[str, Any]:
    """
    퀀트 트레이딩 현황 조회.
    type: signals | portfolio | trades | strategy | all
    days: 신호 조회 기간 (type=signals 일 때)
    limit: 조회 건수 (type=trades/strategy 일 때)
    """
    valid = {"signals", "portfolio", "trades", "strategy", "all"}
    if type not in valid:
        return {"ok": False, "message": f"type must be one of {valid}"}
    try:
        fns = _get_report_fns()
        fn = fns[type]
        if type == "signals":
            output = fn(days=days)
        elif type in ("trades", "strategy"):
            output = fn(limit=limit)
        else:
            output = fn()
        return {"ok": True, "report": output}
    except Exception as e:
        return {"ok": False, "report": str(e)}


@mcp.tool()
def quant_strategy_log(category: str, summary: str, detail: str = "") -> dict[str, Any]:
    """
    전략 결정 기록 및 텔레그램 발송.
    category: trade (매매결정) | watchlist (조건변경) | general (전략메모)
    """
    if category not in {"trade", "watchlist", "general"}:
        return {"ok": False, "message": "category must be trade | watchlist | general"}
    if not summary.strip():
        return {"ok": False, "message": "summary is required"}
    ok = _log_and_notify(category, summary, detail)
    return {"ok": ok, "message": "✅ 전송 완료" if ok else "❌ 전송 실패"}


@mcp.tool()
def quant_portfolio_sync() -> dict[str, Any]:
    """포트폴리오 및 매매내역을 키움 API에서 조회해 DB에 동기화."""
    try:
        from worker.clients.kiwoom_client import KiwoomClient
        from worker.portfolio_sync import sync_all
        kiwoom = KiwoomClient()
        sync_all(kiwoom)
        return {"ok": True, "message": "✅ 포트폴리오 동기화 완료"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_watchlist_read() -> dict[str, Any]:
    """
    모니터링 종목 및 조건 전체 조회 (DB).
    각 종목에 in_portfolio(보유 여부) 필드가 포함되어 보유/미보유 구분 가능.
    """
    try:
        from data.db import get_watchlist, get_portfolio, get_positions
        watchlist = get_watchlist()
        holding_codes = {h["stock_code"] for h in get_portfolio()}
        positions_map = {p["stock_code"]: p for p in get_positions()}
        for stock in watchlist:
            stock["in_portfolio"] = stock["code"] in holding_codes
            if stock["code"] in positions_map:
                stock["position"] = positions_map[stock["code"]]
        return {"ok": True, "watchlist": watchlist}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_watchlist_update(stock_code: str, field: str, value: str) -> dict[str, Any]:
    """
    종목 조건 수정 (DB 즉시 반영).
    stock_code: 종목코드 (예: '012450')
    field: 수정할 필드 (예: 'target_price', 'stop_loss_price', 'rsi_overbought', 'enabled' 등)
    value: 새 값 (숫자 문자열 자동 변환)
    """
    try:
        from data.db import get_watchlist, update_stock_field, get_position, update_position_field
        stocks = {s["code"]: s for s in get_watchlist()}
        if stock_code not in stocks:
            return {"ok": False, "message": f"종목코드 {stock_code} 없음. 등록된 코드: {list(stocks.keys())}"}

        target = stocks[stock_code]

        if value.lower() in ("true", "false"):
            new_value = value.lower() == "true"
        else:
            try:
                new_value = int(float(value)) if "." not in value else float(value)
            except ValueError:
                new_value = value

        # 포지션 전용 필드는 positions 테이블로 라우팅
        position_fields = {"target_price", "stop_loss_price", "add_buy_price", "mid_sell_price",
                           "rsi_oversold_add", "bollinger_lower_break_add", "ma5_recovery_add"}
        if field in position_fields:
            pos = get_position(stock_code)
            if pos:
                old_value = pos.get(field)
                update_position_field(stock_code, field, new_value)
                return {"ok": True, "message": f"✅ {target['name']} 포지션 {field}: {old_value} → {new_value}"}
            else:
                return {"ok": False, "message": f"포지션 없음: {stock_code} (미보유 종목). 매수 후 포지션이 자동 생성됩니다."}

        old_value = target.get(field)
        update_stock_field(stock_code, field, new_value)
        return {"ok": True, "message": f"✅ {target['name']} {field}: {old_value} → {new_value}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_watchlist_add(code: str, name: str, conditions: str = "{}") -> dict[str, Any]:
    """
    모니터링 종목 추가 (DB).
    conditions: JSON 문자열 (예: '{"target_price": 100000, "rsi_overbought": 70}')
    """
    try:
        import json as _json
        from data.db import upsert_stock
        cond_dict = _json.loads(conditions) if conditions else {}
        upsert_stock(str(code), name, True, cond_dict)
        return {"ok": True, "message": f"✅ {name} ({code}) 모니터링 추가 완료."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_watchlist_delete(stock_code: str) -> dict[str, Any]:
    """모니터링 종목 삭제 (DB)."""
    try:
        from data.db import delete_stock, get_watchlist
        stocks = {s["code"]: s for s in get_watchlist()}
        if stock_code not in stocks:
            return {"ok": False, "message": f"종목코드 {stock_code} 없음."}
        name = stocks[stock_code]["name"]
        delete_stock(stock_code)
        return {"ok": True, "message": f"✅ {name} ({stock_code}) 모니터링 삭제 완료."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_positions_read() -> dict[str, Any]:
    """
    보유 종목 포지션 관리 정보 조회 (목표가/손절가/추가매수가/중간매도가).
    매수 후 확정된 포지션 관리 파라미터를 조회합니다.
    """
    try:
        from data.db import get_positions
        return {"ok": True, "positions": get_positions()}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_position_update(stock_code: str, field: str, value: str) -> dict[str, Any]:
    """
    보유 종목 포지션 필드 수정 (DB 즉시 반영).
    stock_code: 종목코드 (예: '012450')
    field: 수정할 필드 (target_price, stop_loss_price, add_buy_price, mid_sell_price, rsi_oversold_add 등)
    value: 새 값 (숫자 문자열 자동 변환)
    """
    try:
        from data.db import get_position, update_position_field
        pos = get_position(stock_code)
        if not pos:
            return {"ok": False, "message": f"포지션 없음: {stock_code} (미보유 종목이거나 포지션 미생성)"}

        old_value = pos.get(field)

        if value.lower() in ("true", "false"):
            new_value = value.lower() == "true"
        elif value.lower() in ("none", "null", ""):
            new_value = None
        else:
            try:
                new_value = int(float(value)) if "." not in value else float(value)
            except ValueError:
                new_value = value

        ok = update_position_field(stock_code, field, new_value)
        if not ok:
            return {"ok": False, "message": f"필드 '{field}'은 수정 불가 (허용: target_price, stop_loss_price, add_buy_price, mid_sell_price, rsi_oversold_add, bollinger_lower_break_add, ma5_recovery_add, strategy_note)"}
        return {"ok": True, "message": f"✅ {pos['stock_name']} {field}: {old_value} → {new_value}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_conditions_list() -> dict[str, Any]:
    """시그널 조건 타입 전체 목록 조회 (DB)."""
    try:
        from data.db import get_conditions
        return {"ok": True, "conditions": get_conditions()}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_condition_add(
    id: str, name: str, evaluator: str, param: str,
    cooldown_minutes: int, message: str,
    chart_field: str = "", description: str = "", signal_type: str = "both",
) -> dict[str, Any]:
    """
    새 시그널 조건 타입 추가 (DB).
    evaluator: price_gte / price_lte / rsi_gte / rsi_lte / volume_gte / flag
    signal_type: entry / exit / both
    """
    valid_evaluators = {"price_gte", "price_lte", "rsi_gte", "rsi_lte", "volume_gte", "flag"}
    if evaluator not in valid_evaluators:
        return {"ok": False, "message": f"evaluator는 {valid_evaluators} 중 하나여야 합니다."}
    if evaluator == "flag" and not chart_field:
        return {"ok": False, "message": "flag evaluator는 chart_field가 필수입니다."}
    if signal_type not in {"entry", "exit", "both"}:
        return {"ok": False, "message": "signal_type은 entry / exit / both 중 하나여야 합니다."}
    try:
        from data.db import add_condition, get_conditions
        if any(c["id"] == id for c in get_conditions()):
            return {"ok": False, "message": f"조건 ID '{id}'가 이미 존재합니다."}
        ok = add_condition({
            "id": id, "name": name, "evaluator": evaluator, "param": param,
            "cooldown_minutes": cooldown_minutes, "message": message,
            "chart_field": chart_field or None, "description": description,
            "signal_type": signal_type,
        })
        if ok:
            _log_and_notify("general", f"시그널 조건 추가: {name}", f"id={id}, evaluator={evaluator}, param={param}")
            return {"ok": True, "message": f"✅ 조건 '{name}' 추가 완료."}
        return {"ok": False, "message": "추가 실패 (중복 ID 가능성)"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_condition_update(
    id: str, name: str = "", cooldown_minutes: int = 0,
    message: str = "", description: str = "",
    signal_type: str = "", chart_field: str = "",
) -> dict[str, Any]:
    """
    기존 시그널 조건 수정 (conditions_def 테이블).
    id: 수정할 조건 ID (필수). 나머지: 변경할 항목만 입력.
    """
    fields = {}
    if name:
        fields["name"] = name
    if cooldown_minutes > 0:
        fields["cooldown_minutes"] = cooldown_minutes
    if message:
        fields["message"] = message
    if description:
        fields["description"] = description
    if signal_type:
        if signal_type not in {"entry", "exit", "both", "add"}:
            return {"ok": False, "message": "signal_type은 entry / exit / both / add 중 하나여야 합니다."}
        fields["signal_type"] = signal_type
    if chart_field:
        fields["chart_field"] = chart_field
    if not fields:
        return {"ok": False, "message": "변경할 항목을 하나 이상 입력해 주세요."}
    try:
        from data.db import update_condition, get_conditions
        target = next((c for c in get_conditions() if c["id"] == id), None)
        if not target:
            return {"ok": False, "message": f"조건 ID '{id}' 없음."}
        ok = update_condition(id, fields)
        if ok:
            changed = ", ".join(f"{k}={v}" for k, v in fields.items())
            _log_and_notify("general", f"시그널 조건 수정: {target['name']}", f"id={id}, {changed}")
            return {"ok": True, "message": f"✅ 조건 '{target['name']}' 수정 완료.", "updated": fields}
        return {"ok": False, "message": "수정 실패"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_condition_remove(id: str) -> dict[str, Any]:
    """시그널 조건 타입 삭제 (DB)."""
    try:
        from data.db import remove_condition, get_conditions
        target = next((c for c in get_conditions() if c["id"] == id), None)
        if not target:
            return {"ok": False, "message": f"조건 ID '{id}' 없음."}
        remove_condition(id)
        _log_and_notify("general", f"시그널 조건 삭제: {target['name']}", f"id={id}")
        return {"ok": True, "message": f"✅ 조건 '{target['name']}' 삭제 완료."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_signal_log_delete(
    signal_id: int = 0, stock_code: str = "",
    before_date: str = "", delete_all: bool = False,
) -> dict[str, Any]:
    """
    신호 로그 삭제 (signals 테이블). conditions_def(조건 정의) 삭제와 무관.
    삭제 방식: signal_id / stock_code / before_date / delete_all 중 하나 지정.
    """
    try:
        from data.db import delete_signal, delete_signals_by_stock, delete_signals_before, delete_all_signals
        if signal_id:
            ok = delete_signal(signal_id)
            return {"ok": ok, "message": f"✅ 신호 ID {signal_id} 삭제 완료." if ok else f"❌ ID {signal_id} 없음."}
        elif stock_code:
            count = delete_signals_by_stock(stock_code)
            return {"ok": True, "message": f"✅ {stock_code} 신호 {count}건 삭제 완료."}
        elif before_date:
            count = delete_signals_before(before_date)
            return {"ok": True, "message": f"✅ {before_date} 이전 신호 {count}건 삭제 완료."}
        elif delete_all:
            count = delete_all_signals()
            return {"ok": True, "message": f"✅ 신호 로그 전체 {count}건 삭제 완료."}
        else:
            return {"ok": False, "message": "삭제 조건을 하나 이상 지정하세요."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_strategy_note_update(
    note_id: int, summary: str = "", detail: str = "", category: str = "",
) -> dict[str, Any]:
    """
    전략 노트 수정 (strategy_notes 테이블).
    note_id: 수정할 노트 ID (필수). 나머지: 변경할 항목만 입력.
    """
    if not any([summary, detail, category]):
        return {"ok": False, "message": "변경할 항목을 하나 이상 입력해 주세요."}
    try:
        from data.db import update_strategy_note, get_strategy_note
        note = get_strategy_note(note_id)
        if not note:
            return {"ok": False, "message": f"전략 노트 ID {note_id} 없음."}
        ok = update_strategy_note(note_id, summary=summary, detail=detail, category=category)
        if ok:
            display = summary or note.get("summary", "")
            _notify("전략 수정", f"[ID:{note_id}] {display}", ts=note.get("created_at", ""))
            return {"ok": True, "message": f"✅ 전략 노트 ID {note_id} 수정 완료."}
        return {"ok": False, "message": "수정 실패"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_strategy_note_delete(note_id: int = 0, delete_all: bool = False) -> dict[str, Any]:
    """
    전략 노트 삭제 (strategy_notes 테이블).
    삭제 방식: note_id / delete_all 중 하나 지정.
    """
    try:
        from data.db import (
            get_strategy_note, get_strategy_notes,
            delete_strategy_note, delete_all_strategy_notes,
        )
        if note_id:
            note = get_strategy_note(note_id)
            ok = delete_strategy_note(note_id)
            if ok and note:
                _notify("전략 삭제", f"[ID:{note_id}] {note.get('summary', '')}", ts=note.get("created_at", ""))
            return {"ok": ok, "message": f"✅ 전략 노트 ID {note_id} 삭제 완료." if ok else f"❌ ID {note_id} 없음."}
        elif delete_all:
            notes = get_strategy_notes(limit=10000)
            count = delete_all_strategy_notes()
            for note in notes:
                nid = note.get("id", "")
                _notify("전략 삭제", f"[ID:{nid}] {note.get('summary', '')}", ts=note.get("created_at", ""))
            return {"ok": True, "message": f"✅ 전략 노트 전체 {count}건 삭제 완료."}
        else:
            return {"ok": False, "message": "삭제 조건을 지정하세요 (note_id / delete_all)."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_cooldown_reset(stock_code: str = "", reset_all: bool = False) -> dict[str, Any]:
    """
    매매 체결 후 쿨다운 리셋.
    stock_code: 특정 종목 쿨다운만 리셋 / reset_all=True: 전체 리셋
    """
    try:
        from data.db import reset_cooldowns_for_stock, reset_all_cooldowns
        if stock_code:
            count = reset_cooldowns_for_stock(stock_code)
            return {"ok": True, "message": f"✅ {stock_code} 쿨다운 {count}건 리셋 완료."}
        elif reset_all:
            count = reset_all_cooldowns()
            return {"ok": True, "message": f"✅ 전체 쿨다운 {count}건 리셋 완료."}
        else:
            return {"ok": False, "message": "stock_code 또는 reset_all=True 중 하나를 지정하세요."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ═══════════════════════════ DART 공시 도구 ═══════════════════════════

@mcp.tool()
def dart_disclosures(
    stock_code: str, days: int = 30, pblntf_ty: str = "",
) -> dict[str, Any]:
    """종목별 DART 공시 목록 조회.
    stock_code: 종목코드 6자리, days: 조회 기간, pblntf_ty: 공시유형 필터
    """
    try:
        from kiwoom_mcp.dart_client import search_disclosures
        bgn = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        data = search_disclosures(stock_code=stock_code, bgn_de=bgn, end_de=end, pblntf_ty=pblntf_ty)
        items = data.get("list", [])
        return {"status": "ok", "count": len(items), "total": data.get("total_count", 0), "disclosures": items[:30]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def dart_company_info(stock_code: str) -> dict[str, Any]:
    """DART 기업개황 조회 (대표자, 업종, 설립일, 홈페이지 등)."""
    try:
        from kiwoom_mcp.dart_client import get_company_info
        return {"status": "ok", "company": get_company_info(stock_code)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def dart_financial(
    stock_code: str, year: str = "", report: str = "11011", detail: str = "summary",
) -> dict[str, Any]:
    """DART 재무정보 조회.
    detail: summary=주요계정, all=전체재무제표, index=재무지표(ROE/PER 등)
    """
    try:
        from kiwoom_mcp.dart_client import get_financial_single, get_financial_all, get_financial_index
        if detail == "all":
            return {"status": "ok", "data": get_financial_all(stock_code, year, report)}
        elif detail == "index":
            return {"status": "ok", "data": get_financial_index(stock_code, year, report)}
        else:
            return {"status": "ok", "data": get_financial_single(stock_code, year, report)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def dart_shareholders(stock_code: str, type: str = "major") -> dict[str, Any]:
    """DART 지분공시 조회. type: major=대량보유(5%+), executive=임원·주요주주"""
    try:
        from kiwoom_mcp.dart_client import get_major_shareholders, get_executive_shareholders
        if type == "executive":
            return {"status": "ok", "data": get_executive_shareholders(stock_code)}
        else:
            return {"status": "ok", "data": get_major_shareholders(stock_code)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def dart_periodic_report(
    stock_code: str, report_type: str, year: str = "", reprt_code: str = "11011",
) -> dict[str, Any]:
    """DART 정기보고서 주요정보 조회 (28종)."""
    try:
        from kiwoom_mcp.dart_client import get_periodic_report
        return {"status": "ok", "data": get_periodic_report(stock_code, report_type, year, reprt_code)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def dart_major_event(
    stock_code: str, event_type: str, bgn_de: str = "", end_de: str = "",
) -> dict[str, Any]:
    """DART 주요사항보고서 조회 (36종)."""
    try:
        from kiwoom_mcp.dart_client import get_major_event
        return {"status": "ok", "data": get_major_event(stock_code, event_type, bgn_de, end_de)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
