from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DepositRecord(BaseModel):
    id: str
    account_no: str
    amount: float
    direction: str
    occurred_at: datetime
    description: str = ''


class TradeRecord(BaseModel):
    id: str
    account_no: str
    symbol: str
    side: str
    quantity: float
    price: float
    occurred_at: datetime
    gross_amount: float | None = None
    fee: float | None = None
    tax: float | None = None
    net_amount: float | None = None
    order_no: str = ''
    trade_no: str = ''
    trade_kind: str = ''
    io_type_code: str = ''
    note: str = ''


class AccountRecord(BaseModel):
    account_no: str
    cash_balance: float
    occurred_at: datetime
