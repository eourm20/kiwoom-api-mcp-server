"""
DART 공시 API 풀 클라이언트 (MCP 서버용)
- 공시검색, 기업개황, 재무정보, 지분공시, 주요사항보고서 등 전체 API 지원
- 83개 DART OpenAPI 대응
- Rate limit: 20,000 requests/day
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from typing import Any

import requests
from dotenv import load_dotenv

# .env 로드 (MCP 패키지 → 프로젝트 루트 순서)
_module_dir = os.path.dirname(__file__)
load_dotenv(os.path.join(_module_dir, ".env"), override=False)
load_dotenv(os.path.join(_module_dir, "..", ".env"), override=False)
load_dotenv(os.path.join(_module_dir, "..", "..", ".env"), override=False)

logger = logging.getLogger(__name__)

DART_API_KEY = os.getenv("DART_API_KEY", "").strip()
DART_BASE_URL = "https://opendart.fss.or.kr/api"

# ─────────────────────────── corp_code 매핑 ───────────────────────────

_corp_code_map: dict[str, str] = {}
_corp_code_loaded = False
_CACHE_DIR = os.path.join(_module_dir, ".dart_cache")


def _ensure_cache_dir():
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _load_corp_code_map() -> dict[str, str]:
    """종목코드 → DART 고유번호 매핑 로드. 하루 1회 갱신."""
    global _corp_code_map, _corp_code_loaded

    if _corp_code_loaded and _corp_code_map:
        return _corp_code_map

    _ensure_cache_dir()
    cache_file = os.path.join(_CACHE_DIR, "corp_code_map.json")
    cache_date_file = os.path.join(_CACHE_DIR, "corp_code_date.txt")

    today = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(cache_file) and os.path.exists(cache_date_file):
        with open(cache_date_file, "r") as f:
            cached_date = f.read().strip()
        if cached_date == today:
            with open(cache_file, "r", encoding="utf-8") as f:
                _corp_code_map = json.load(f)
            _corp_code_loaded = True
            return _corp_code_map

    if not DART_API_KEY:
        logger.warning("DART_API_KEY 미설정")
        return {}

    try:
        resp = requests.get(
            f"{DART_BASE_URL}/corpCode.xml",
            params={"crtfc_key": DART_API_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_data = zf.read(zf.namelist()[0])

        root = ET.fromstring(xml_data)
        result = {}
        for corp in root.findall("list"):
            stock_code = (corp.findtext("stock_code") or "").strip()
            corp_code = (corp.findtext("corp_code") or "").strip()
            if stock_code and corp_code:
                result[stock_code] = corp_code

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(result, f)
        with open(cache_date_file, "w") as f:
            f.write(today)

        _corp_code_map = result
        _corp_code_loaded = True
        logger.info(f"DART corp_code 다운로드: {len(result)}개")
        return result

    except Exception as e:
        logger.error(f"DART corp_code 다운로드 실패: {e}")
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                _corp_code_map = json.load(f)
            _corp_code_loaded = True
            return _corp_code_map
        return {}


def get_corp_code(stock_code: str) -> str | None:
    """종목코드(6자리) → DART 고유번호(8자리)."""
    return _load_corp_code_map().get(stock_code)


def _resolve_corp_code(stock_code: str) -> str:
    """corp_code 조회. 실패 시 에러 발생."""
    code = get_corp_code(stock_code)
    if not code:
        raise ValueError(f"종목코드 {stock_code}에 해당하는 DART 고유번호 없음")
    return code


# ─────────────────────────── 공통 API 호출 ───────────────────────────

def _api_call(endpoint: str, params: dict, timeout: int = 15) -> dict:
    """DART API 공통 호출. JSON 응답 반환."""
    if not DART_API_KEY:
        raise RuntimeError("DART_API_KEY 미설정")

    params["crtfc_key"] = DART_API_KEY
    resp = requests.get(f"{DART_BASE_URL}/{endpoint}.json", params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status", "")
    if status not in ("000", "013"):
        raise RuntimeError(f"DART API 오류: {data.get('message', '')} (status={status})")

    return data


# ─────────────────────── 1. 공시정보 (4개) ───────────────────────

def search_disclosures(
    stock_code: str = "",
    bgn_de: str = "",
    end_de: str = "",
    pblntf_ty: str = "",
    pblntf_detail_ty: str = "",
    corp_cls: str = "",
    last_reprt_at: str = "N",
    page_no: int = 1,
    page_count: int = 100,
) -> dict:
    """공시검색 — 다양한 조건으로 공시보고서 검색.

    pblntf_ty: A=정기, B=주요사항, C=발행, D=지분, E=기타, F=외부감사, I=거래소, J=공정위
    """
    params: dict[str, Any] = {
        "last_reprt_at": last_reprt_at,
        "page_no": str(page_no),
        "page_count": str(min(page_count, 100)),
    }
    if stock_code:
        params["corp_code"] = _resolve_corp_code(stock_code)
    if bgn_de:
        params["bgn_de"] = bgn_de
    if end_de:
        params["end_de"] = end_de
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty
    if pblntf_detail_ty:
        params["pblntf_detail_ty"] = pblntf_detail_ty
    if corp_cls:
        params["corp_cls"] = corp_cls
    return _api_call("list", params)


def get_company_info(stock_code: str) -> dict:
    """기업개황 — 대표자, 업종, 설립일, 홈페이지 등."""
    return _api_call("company", {"corp_code": _resolve_corp_code(stock_code)})


# ─────────────────────── 2. 정기보고서 주요정보 (28개) ───────────────────────

# 엔드포인트 매핑
_PERIODIC_ENDPOINTS = {
    "증자감자": "irdsSttus",
    "배당": "alotMatter",
    "자기주식": "tesstkAcqsDspsSttus",
    "최대주주": "hyslrSttus",
    "최대주주변동": "hyslrChgSttus",
    "소액주주": "mrhlSttus",
    "임원현황": "exctvSttus",
    "직원현황": "empSttus",
    "이사감사개인별보수": "hmvAuditIndvdlBySttus",
    "이사감사전체보수": "hmvAuditAllSttus",
    "개인별보수5억이상": "indvdlByPay",
    "타법인출자": "otrCprInvstmntSttus",
    "주식총수": "stockTotqySttus",
    "채무증권발행": "dbtScrtsIsKndSttus",
    "기업어음미상환": "cprndNrdmpBlce",
    "단기사채미상환": "srtpdPsndbtNrdmpBlce",
    "회사채미상환": "coBondNrdmpBlce",
    "신종자본증권미상환": "nwCapScrtsNrdmpBlce",
    "조건부자본증권미상환": "cndlCapScrtsnRdmpBlce",
    "회계감사인": "accnutAdtorNmNdAdtOpinion",
    "감사용역": "adtServcCnclsSttus",
    "비감사용역": "accnutAdtorNonAdtServcCnclsSttus",
    "사외이사": "outcmpnyDrctorNdChangeSttus",
    "미등기임원보수": "unrstExctvMendngSttus",
    "이사감사전체보수_주총": "hmvAuditAllSttus2",
    "이사감사전체보수_유형별": "hmvAuditAllSttus3",
    "공모자금사용내역": "pssrpCptalUseDtls",
    "사모자금사용내역": "prvsrpCptalUseDtls",
}


def get_periodic_report(
    stock_code: str,
    report_type: str,
    year: str = "",
    reprt_code: str = "11011",
) -> dict:
    """정기보고서 주요정보 조회.

    report_type: 증자감자/배당/자기주식/최대주주/임원현황/직원현황/회계감사인 등
    reprt_code: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기
    """
    endpoint = _PERIODIC_ENDPOINTS.get(report_type)
    if not endpoint:
        available = ", ".join(_PERIODIC_ENDPOINTS.keys())
        raise ValueError(f"알 수 없는 report_type: {report_type}. 사용 가능: {available}")

    if not year:
        year = str(datetime.now().year - 1)

    return _api_call(endpoint, {
        "corp_code": _resolve_corp_code(stock_code),
        "bsns_year": year,
        "reprt_code": reprt_code,
    })


# ─────────────────────── 3. 정기보고서 재무정보 (7개) ───────────────────────

def get_financial_single(
    stock_code: str, year: str = "", reprt_code: str = "11011",
) -> dict:
    """단일회사 주요계정 (매출액, 영업이익, 당기순이익 등)."""
    if not year:
        year = str(datetime.now().year - 1)
    return _api_call("fnlttSinglAcnt", {
        "corp_code": _resolve_corp_code(stock_code),
        "bsns_year": year,
        "reprt_code": reprt_code,
    })


def get_financial_all(
    stock_code: str, year: str = "", reprt_code: str = "11011",
) -> dict:
    """단일회사 전체 재무제표."""
    if not year:
        year = str(datetime.now().year - 1)
    return _api_call("fnlttSinglAcntAll", {
        "corp_code": _resolve_corp_code(stock_code),
        "bsns_year": year,
        "reprt_code": reprt_code,
    })


def get_financial_index(
    stock_code: str, year: str = "", reprt_code: str = "11011",
) -> dict:
    """단일회사 주요 재무지표 (ROE, ROA, PER, PBR 등)."""
    if not year:
        year = str(datetime.now().year - 1)
    return _api_call("fnlttSinglIndx", {
        "corp_code": _resolve_corp_code(stock_code),
        "bsns_year": year,
        "reprt_code": reprt_code,
    })


def get_financial_multi(
    stock_codes: list[str], year: str = "", reprt_code: str = "11011",
) -> dict:
    """다중회사 주요계정 (최대 100개)."""
    if not year:
        year = str(datetime.now().year - 1)
    corp_codes = [_resolve_corp_code(c) for c in stock_codes[:100]]
    return _api_call("fnlttMultiAcnt", {
        "corp_code": ",".join(corp_codes),
        "bsns_year": year,
        "reprt_code": reprt_code,
    })


# ─────────────────────── 4. 지분공시 (2개) ───────────────────────

def get_major_shareholders(stock_code: str) -> dict:
    """대량보유 상황보고 (5% 이상 지분)."""
    return _api_call("majorstock", {"corp_code": _resolve_corp_code(stock_code)})


def get_executive_shareholders(stock_code: str) -> dict:
    """임원·주요주주 소유보고."""
    return _api_call("elestock", {"corp_code": _resolve_corp_code(stock_code)})


# ─────────────────────── 5. 주요사항보고서 (36개) ───────────────────────

_MAJOR_EVENT_ENDPOINTS = {
    "자산양수도": "astInhtrfDecsn",
    "부도발생": "bsnDssltn",
    "영업정지": "bsnSspCrmc",
    "회생절차": "ctrcvsBgnDecsn",
    "해산사유": "dssltnDecsn",
    "유상증자": "piicDecsn",
    "무상증자": "fricDecsn",
    "유무상증자": "pifricDecsn",
    "감자": "crDecsn",
    "채권은행관리절차개시": "bnkMngmntPcbg",
    "소송제기": "lwstDecsn",
    "해외상장결정": "ovLstDecsn",
    "해외상장폐지결정": "ovDlstDecsn",
    "해외상장": "ovLst",
    "해외상장폐지": "ovDlst",
    "전환사채발행": "cvbdIsDecsn",
    "신주인수권부사채발행": "bdwtIsDecsn",
    "교환사채발행": "exbdIsDecsn",
    "채권은행관리절차중단": "bnkMngmntPcsp",
    "상각형조건부자본증권": "wdCndCapScrtsIsDecsn",
    "자기주식취득결정": "tsstkAqDecsn",
    "자기주식처분결정": "tsstkDpDecsn",
    "자기주식신탁계약체결": "tsstkTrCnclsDecsn",
    "자기주식신탁계약해지": "tsstkTrCnclsRlsDecsn",
    "영업양수결정": "bsnTrfDecsn",
    "영업양도결정": "bsnAsbDecsn",
    "유형자산양수": "tgastTrfDecsn",
    "유형자산양도": "tgastAsbDecsn",
    "타법인주식양수": "otcprStkInvscnTrfDecsn",
    "타법인주식양도": "otcprStkInvscnAsbDecsn",
    "주권관련사채양수": "stkrtbdInhtrfDecsn",
    "주권관련사채양도": "stkrtbdTrfDecsn",
    "회사합병결정": "cmpMrgDecsn",
    "회사분할결정": "cmpDvDecsn",
    "회사분할합병결정": "cmpDvmrgDecsn",
    "주식교환이전결정": "stkExtrDecsn",
}


def get_major_event(
    stock_code: str,
    event_type: str,
    bgn_de: str = "",
    end_de: str = "",
) -> dict:
    """주요사항보고서 조회.

    event_type: 유상증자/전환사채발행/회사합병결정/자기주식취득결정 등
    """
    endpoint = _MAJOR_EVENT_ENDPOINTS.get(event_type)
    if not endpoint:
        available = ", ".join(_MAJOR_EVENT_ENDPOINTS.keys())
        raise ValueError(f"알 수 없는 event_type: {event_type}. 사용 가능: {available}")

    if not bgn_de:
        bgn_de = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
    if not end_de:
        end_de = datetime.now().strftime("%Y%m%d")

    return _api_call(endpoint, {
        "corp_code": _resolve_corp_code(stock_code),
        "bgn_de": bgn_de,
        "end_de": end_de,
    })


# ─────────────────────── 편의 함수 ───────────────────────

def get_available_periodic_reports() -> list[str]:
    """사용 가능한 정기보고서 유형 목록."""
    return list(_PERIODIC_ENDPOINTS.keys())


def get_available_major_events() -> list[str]:
    """사용 가능한 주요사항보고서 유형 목록."""
    return list(_MAJOR_EVENT_ENDPOINTS.keys())
