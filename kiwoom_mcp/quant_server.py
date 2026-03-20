"""
Quant Trading MCP 서버 — quant 도구 전용 (report / strategy_log / portfolio_sync)
subprocess 대신 직접 함수 호출로 빠르게 처리.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server import FastMCP
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_module_dir = Path(__file__).resolve().parent
load_dotenv(_module_dir / ".env", override=False)
load_dotenv(_module_dir.parent / ".env", override=False)

# quant_trading 루트를 sys.path에 추가
_quant_root = Path(os.getenv("QUANT_TRADING_PATH", "")).resolve() if os.getenv("QUANT_TRADING_PATH") else _module_dir.parent.parent.parent
if str(_quant_root) not in sys.path:
    sys.path.insert(0, str(_quant_root))

# quant_trading .env 로드
load_dotenv(_quant_root / ".env", override=False)

mcp = FastMCP("quant-mcp")

# DB 초기화 (테이블 없으면 생성, 이미 있으면 무시)
try:
    from data.db import init_db
    init_db()
except Exception as e:
    logger.warning(f"[quant-mcp] DB 초기화 실패: {e}")


def _is_trading_hours() -> bool:
    """평일 08:00~16:00 (프리장 포함)"""
    now = datetime.now()
    return now.weekday() < 5 and 8 <= now.hour < 16


def _auto_sync_loop(interval_seconds: int = 600):
    """Claude Desktop 실행 중 장 시간대 포트폴리오 자동 동기화 (백그라운드 스레드)."""
    time.sleep(30)  # 서버 초기화 대기
    while True:
        try:
            if _is_trading_hours():
                from worker.clients.kiwoom_client import KiwoomClient
                from worker.portfolio_sync import sync_all
                sync_all(KiwoomClient())
                logger.info("[quant-mcp] 포트폴리오 자동 동기화 완료")
        except Exception as e:
            logger.warning(f"[quant-mcp] 자동 동기화 실패: {e}")
        time.sleep(interval_seconds)


# Claude Desktop 실행 중 백그라운드 자동 동기화 시작 (10분 간격)
_sync_thread = threading.Thread(target=_auto_sync_loop, daemon=True, name="quant-auto-sync")
_sync_thread.start()


_CATEGORY_LABEL = {"trade": "매매 결정", "watchlist": "조건 변경", "general": "전략 메모"}
_ACTION_EMOJI = {
    "매매 결정": "💼", "조건 변경": "⚙️", "전략 메모": "📋",
    "전략 삭제": "🗑", "전략 추가": "📋",
}

# 텔레그램 발송 버퍼 — 짧은 시간 내 여러 변경을 모아 1건으로 발송
_notify_buffer: list[tuple[str, str, str]] = []  # [(action, summary, ts), ...]
_notify_timer: threading.Timer | None = None
_notify_lock = threading.Lock()
_NOTIFY_DELAY = 4.0  # 초 — 마지막 변경 후 이 시간이 지나면 발송


def _flush_notify_buffer() -> None:
    """버퍼에 쌓인 알림을 하나의 메시지로 묶어 발송.
    같은 액션 타입은 N건으로 묶어 표시 (예: 전략 삭제 8건).
    타임스탬프는 각 항목이 실제 처리된 시점 기준.
    """
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

    # 단건
    if len(items) == 1:
        action, summary, ts = items[0]
        _telegram_send(_fmt(action, summary, ts))
        return

    # 다건: 같은 액션끼리 묶어서 N건으로 표시
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
    """Telegram Markdown V1 특수문자 이스케이프 (동적 콘텐츠에만 적용)."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _telegram_send(text: str) -> bool:
    """quant_server.py 자체 텔레그램 발송 — 외부 모듈 의존 없이 httpx 직접 호출.
    env 값은 호출 시점에 읽어서 워커 없이도 동작.
    Markdown 파싱 실패 시 plain text로 재시도.
    """
    import httpx
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("[quant-mcp] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정")
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
        # Markdown 파싱 오류(400)면 plain text로 재시도
        logger.warning(f"[quant-mcp] Markdown 발송 실패({resp.status_code}), plain text 재시도")
        resp2 = _post(None)
        ok = resp2.status_code == 200
        if not ok:
            logger.warning(f"[quant-mcp] plain text 발송도 실패: {resp2.status_code} {resp2.text[:200]}")
        return ok
    except Exception as e:
        logger.warning(f"[quant-mcp] 텔레그램 발송 실패: {e}")
        return False


def _notify(action: str, summary: str, detail: str = "", ts: str = "") -> bool:
    """텔레그램 버퍼에 추가 — _NOTIFY_DELAY 초 후 일괄 발송.
    ts: 노트의 created_at 값 (없으면 현재 시각 사용)
    """
    global _notify_timer
    if not ts:
        ts = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    else:
        # DB 포맷(2026-03-18 12:03:05) → 표시 포맷(2026.03.18 12:03:05)
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
    """DB 저장 + 텔레그램 발송. 저장된 노트의 created_at과 id를 타임스탬프/표시에 사용."""
    created_at = ""
    display_summary = summary
    try:
        from data.db import save_strategy_note, get_strategy_notes
        save_strategy_note(category, summary, detail)
        # 방금 저장한 노트의 id/created_at 조회 (가장 최근 1건)
        notes = get_strategy_notes(limit=1)
        if notes:
            created_at = notes[0].get("created_at", "")
            note_id = notes[0].get("id", "")
            if note_id:
                display_summary = f"[ID:{note_id}] {summary}"
    except Exception as e:
        logger.warning(f"[quant-mcp] strategy_note 저장 실패: {e}")

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
    '관심종목 기준점', '모니터링 조건', '감시 종목', '관심종목 설정',
    '목표가/손절가 확인', '어떤 종목 보고 있어', 'watchlist' 등의 요청에 사용.
    각 종목에 in_portfolio(보유 여부) 필드가 포함되어 보유/미보유 구분 가능.
    """
    try:
        from data.db import get_watchlist, get_portfolio
        watchlist = get_watchlist()
        holding_codes = {h["stock_code"] for h in get_portfolio()}
        for stock in watchlist:
            stock["in_portfolio"] = stock["code"] in holding_codes
        return {"ok": True, "watchlist": watchlist}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_watchlist_update(stock_code: str, field: str, value: str) -> dict[str, Any]:
    """
    종목 조건 수정 (DB 즉시 반영, 워커 다음 체크 시 자동 적용).
    '목표가 바꿔줘', '손절가 낮춰줘', 'RSI 기준 바꿔줘', '모니터링 꺼줘' 등에 사용.
    stock_code: 종목코드 (예: '012450')
    field: 수정할 필드 (예: 'target_price', 'stop_loss_price', 'rsi_overbought', 'enabled' 등)
    value: 새 값 (숫자 문자열 자동 변환)
    """
    try:
        from data.db import get_watchlist, update_stock_field
        stocks = {s["code"]: s for s in get_watchlist()}
        if stock_code not in stocks:
            return {"ok": False, "message": f"종목코드 {stock_code} 없음. 등록된 코드: {list(stocks.keys())}"}

        target = stocks[stock_code]
        old_value = target.get(field) or target.get("conditions", {}).get(field)

        # 타입 자동 변환
        if value.lower() in ("true", "false"):
            new_value = value.lower() == "true"
        else:
            try:
                new_value = int(float(value)) if "." not in value else float(value)
            except ValueError:
                new_value = value

        update_stock_field(stock_code, field, new_value)
        return {"ok": True, "message": f"✅ {target['name']} {field}: {old_value} → {new_value}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_watchlist_add(code: str, name: str, conditions: str = "{}") -> dict[str, Any]:
    """
    모니터링 종목 추가 (DB).
    '종목 추가해줘', '이 종목도 모니터링해줘' 등에 사용.
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
    """
    모니터링 종목 삭제 (DB).
    '이 종목 모니터링 제거해줘', '감시 목록에서 빼줘' 등에 사용.
    """
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
def quant_conditions_list() -> dict[str, Any]:
    """
    시그널 조건 타입 전체 목록 조회 (DB).
    '어떤 조건으로 신호 보내?', '조건 종류 알려줘', '어떤 지표 쓰고 있어?' 등에 사용.
    """
    try:
        from data.db import get_conditions
        return {"ok": True, "conditions": get_conditions()}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_condition_add(
    id: str,
    name: str,
    evaluator: str,
    param: str,
    cooldown_minutes: int,
    message: str,
    chart_field: str = "",
    description: str = "",
    signal_type: str = "both",
) -> dict[str, Any]:
    """
    새 시그널 조건 타입 추가 (DB).
    '새 조건 추가해줘', 'MACD 조건 만들어줘' 등에 사용.

    evaluator: price_gte / price_lte / rsi_gte / rsi_lte / volume_gte / flag
    flag의 chart_field: golden_cross, death_cross, new_high_20d, broke_below_ma20,
      broke_below_ma5, broke_above_ma5, macd_golden_cross, macd_death_cross,
      bollinger_above_upper, bollinger_below_lower
    message 포맷 키: {price} {threshold} {rsi} {ratio} {ma5} {ma20} {macd} {signal} {upper} {lower}
    signal_type: entry (미보유 매수 타이밍) / exit (보유 중 매도·관리) / both (물타기 포함 항상)

    추가 후 각 종목에 quant_watchlist_update로 param 필드를 설정해야 활성화됨.
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
            return {"ok": True, "message": f"✅ 조건 '{name}' 추가 완료. 각 종목에 '{param}' 필드를 설정해야 활성화됩니다."}
        return {"ok": False, "message": "추가 실패 (중복 ID 가능성)"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_condition_update(
    id: str,
    name: str = "",
    cooldown_minutes: int = 0,
    message: str = "",
    description: str = "",
    signal_type: str = "",
    chart_field: str = "",
) -> dict[str, Any]:
    """
    기존 시그널 조건 수정 (conditions_def 테이블).
    '조건 쿨다운 바꿔줘', 'RSI 조건 메시지 수정해줘', '조건 설명 업데이트해줘' 등에 사용.

    id: 수정할 조건 ID (필수)
    나머지 파라미터: 변경할 항목만 입력 (빈 값/0은 변경 안 함)
    signal_type: entry / exit / both / add
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
    """
    시그널 조건 타입 삭제 (DB).
    '조건 삭제해줘', '이 조건 없애줘' 등에 사용.
    """
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
    signal_id: int = 0,
    stock_code: str = "",
    before_date: str = "",
    delete_all: bool = False,
) -> dict[str, Any]:
    """
    신호 로그 삭제 (signals 테이블).
    '신호 기록 지워줘', '이 종목 신호 이력 삭제해줘', '오래된 신호 정리해줘' 등에 사용.
    conditions_def(조건 정의) 삭제와 무관 — 발생 이력만 삭제.

    삭제 방식 (하나만 지정):
    - signal_id: 특정 신호 1건 삭제 (id 값 사용)
    - stock_code: 특정 종목의 신호 전체 삭제
    - before_date: 해당 날짜 이전 신호 삭제 (예: '2025-01-01')
    - delete_all=True: 전체 삭제
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
            return {"ok": False, "message": "삭제 조건을 하나 이상 지정하세요 (signal_id / stock_code / before_date / delete_all)."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_strategy_note_update(
    note_id: int,
    summary: str = "",
    detail: str = "",
    category: str = "",
) -> dict[str, Any]:
    """
    전략 노트 수정 (strategy_notes 테이블).
    '전략 노트 수정해줘', '노트 내용 바꿔줘' 등에 사용.

    note_id: 수정할 노트 ID (필수) — quant_report(type="strategy")로 확인
    나머지: 변경할 항목만 입력 (빈 값은 변경 안 함)
    category: trade / watchlist / general
    """
    if not any([summary, detail, category]):
        return {"ok": False, "message": "변경할 항목을 하나 이상 입력해 주세요 (summary / detail / category)."}
    try:
        from data.db import update_strategy_note, get_strategy_note
        note = get_strategy_note(note_id)
        if not note:
            return {"ok": False, "message": f"전략 노트 ID {note_id} 없음."}
        ok = update_strategy_note(note_id, summary=summary, detail=detail, category=category)
        if ok:
            updated = get_strategy_note(note_id)
            display = summary or note.get("summary", "")
            _notify("전략 수정", f"[ID:{note_id}] {display}", ts=note.get("created_at", ""))
            return {"ok": True, "message": f"✅ 전략 노트 ID {note_id} 수정 완료."}
        return {"ok": False, "message": "수정 실패"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_strategy_note_delete(
    note_id: int = 0,
    delete_all: bool = False,
) -> dict[str, Any]:
    """
    전략 노트 삭제 (strategy_notes 테이블).
    '전략 노트 N번 지워줘', '전략 노트 전부 지워줘' 등에 사용.

    삭제 방식 (하나만 지정):
    - note_id: 특정 노트 1건 삭제 — quant_report(type="strategy")로 ID 확인
    - delete_all=True: 전체 삭제
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
    매매 체결 후 쿨다운 리셋 — 새 포지션 기준으로 신호를 다시 시작.
    '쿨다운 리셋해줘', '신호 다시 받고 싶어', '매수/매도 후 쿨다운 초기화' 등에 사용.

    - stock_code: 특정 종목 쿨다운만 리셋 (매매 후 자동 호출)
    - reset_all=True: 전체 종목 쿨다운 리셋 (장 시작 외 수동 초기화)
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

    Args:
        stock_code: 종목코드 6자리 (예: "005930")
        days: 조회 기간 (일, 기본 30일)
        pblntf_ty: 공시유형 필터 (빈값=전체, A=정기, B=주요사항, C=발행, D=지분, E=기타, I=거래소)
    """
    try:
        from kiwoom_mcp.dart_client import search_disclosures
        from datetime import datetime, timedelta
        bgn = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        data = search_disclosures(stock_code=stock_code, bgn_de=bgn, end_de=end, pblntf_ty=pblntf_ty)
        items = data.get("list", [])
        return {"status": "ok", "count": len(items), "total": data.get("total_count", 0), "disclosures": items[:30]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def dart_company_info(stock_code: str) -> dict[str, Any]:
    """DART 기업개황 조회 (대표자, 업종, 설립일, 홈페이지 등).

    Args:
        stock_code: 종목코드 6자리
    """
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

    Args:
        stock_code: 종목코드 6자리
        year: 사업연도 (빈값=전년도)
        report: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기
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
    """DART 지분공시 조회.

    Args:
        stock_code: 종목코드 6자리
        type: major=대량보유(5%+), executive=임원·주요주주 소유
    """
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
    """DART 정기보고서 주요정보 조회 (28종).

    Args:
        stock_code: 종목코드 6자리
        report_type: 증자감자/배당/자기주식/최대주주/임원현황/직원현황/회계감사인 등
        year: 사업연도 (빈값=전년도)
        reprt_code: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기

    사용 가능한 report_type: 증자감자, 배당, 자기주식, 최대주주, 최대주주변동, 소액주주,
    임원현황, 직원현황, 이사감사개인별보수, 이사감사전체보수, 개인별보수5억이상,
    타법인출자, 주식총수, 채무증권발행, 기업어음미상환, 단기사채미상환, 회사채미상환,
    신종자본증권미상환, 조건부자본증권미상환, 회계감사인, 감사용역, 비감사용역,
    사외이사, 미등기임원보수, 이사감사전체보수_주총, 이사감사전체보수_유형별,
    공모자금사용내역, 사모자금사용내역
    """
    try:
        from kiwoom_mcp.dart_client import get_periodic_report
        return {"status": "ok", "data": get_periodic_report(stock_code, report_type, year, reprt_code)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def dart_major_event(
    stock_code: str, event_type: str, bgn_de: str = "", end_de: str = "",
) -> dict[str, Any]:
    """DART 주요사항보고서 조회 (36종).

    Args:
        stock_code: 종목코드 6자리
        event_type: 유상증자/전환사채발행/회사합병결정/자기주식취득결정 등
        bgn_de: 시작일 YYYYMMDD (빈값=1년 전)
        end_de: 종료일 YYYYMMDD (빈값=오늘)

    사용 가능한 event_type: 자산양수도, 부도발생, 영업정지, 회생절차, 해산사유,
    유상증자, 무상증자, 유무상증자, 감자, 채권은행관리절차개시, 소송제기,
    해외상장결정, 해외상장폐지결정, 전환사채발행, 신주인수권부사채발행, 교환사채발행,
    자기주식취득결정, 자기주식처분결정, 영업양수결정, 영업양도결정,
    유형자산양수, 유형자산양도, 타법인주식양수, 타법인주식양도,
    회사합병결정, 회사분할결정, 회사분할합병결정, 주식교환이전결정
    """
    try:
        from kiwoom_mcp.dart_client import get_major_event
        return {"status": "ok", "data": get_major_event(stock_code, event_type, bgn_de, end_de)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
