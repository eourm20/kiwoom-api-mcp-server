"""
Quant Trading MCP 서버 — quant 도구 전용 (report / strategy_log / portfolio_sync)
subprocess 대신 직접 함수 호출로 빠르게 처리.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from mcp.server import FastMCP
from dotenv import load_dotenv

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
    try:
        from worker.strategy_log import log_strategy
        ok = log_strategy(category, summary, detail)
        return {"ok": ok, "message": "✅ 전송 완료" if ok else "❌ 전송 실패"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@mcp.tool()
def quant_portfolio_sync() -> dict[str, Any]:
    """포트폴리오 및 매매내역을 키움 API에서 조회해 DB에 동기화."""
    try:
        from worker.kiwoom_client import KiwoomClient
        from worker.portfolio_sync import sync_all
        kiwoom = KiwoomClient()
        sync_all(kiwoom)
        return {"ok": True, "message": "✅ 포트폴리오 동기화 완료"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
