from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from kiwoom_mcp.models import AccountRecord, DepositRecord, TradeRecord

logger = logging.getLogger(__name__)

KST = ZoneInfo('Asia/Seoul')


class KiwoomRestClient:
    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        account_no: str,
        token_path: str,
        account_path: str,
        ws_base_url: str,
        realtime_path: str,
        deposits_api_id: str,
        trades_api_id: str,
        account_balance_api_id: str,
        dmst_stex_tp: str,
        gds_tp: str,
        crnc_cd: str,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip('/')
        self._app_key = app_key
        self._app_secret = app_secret
        self._account_no = account_no
        self._token_path = token_path
        self._account_path = account_path
        self._ws_base_url = ws_base_url.rstrip('/')
        self._realtime_path = realtime_path
        self._deposits_api_id = deposits_api_id
        self._trades_api_id = trades_api_id
        self._account_balance_api_id = account_balance_api_id
        self._dmst_stex_tp = dmst_stex_tp
        self._gds_tp = gds_tp
        self._crnc_cd = crnc_cd
        self._client = httpx.Client(timeout=timeout)
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    def close(self) -> None:
        self._client.close()

    def execute_api(
        self,
        *,
        api_id: str,
        body: dict[str, Any] | None = None,
        path: str | None = None,
        cont_yn: str = 'N',
        next_key: str = '',
        max_pages: int = 1,
    ) -> dict[str, Any]:
        """Execute an arbitrary Kiwoom API request and return raw page payloads."""
        target_path = (path or self._account_path).strip()
        if not target_path:
            raise ValueError('path is required')
        if not api_id.strip():
            raise ValueError('api_id is required')

        pages: list[dict[str, Any]] = []
        current_cont_yn = (cont_yn or 'N').strip().upper()
        current_next_key = next_key or ''

        for _ in range(max(1, max_pages)):
            headers = self._request_headers(api_id=api_id.strip())
            headers['cont-yn'] = current_cont_yn
            headers['next-key'] = current_next_key

            response = self._client.post(
                self._build_url(target_path),
                json=body or {},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            self._check_return_code(payload)

            next_cont_yn = response.headers.get('cont-yn', 'N')
            next_next_key = response.headers.get('next-key', '')
            pages.append(
                {
                    'status_code': response.status_code,
                    'cont_yn': next_cont_yn,
                    'next_key': next_next_key,
                    'payload': payload,
                }
            )

            if next_cont_yn != 'Y' or not next_next_key:
                break
            current_cont_yn = 'Y'
            current_next_key = next_next_key

        return {
            'api_id': api_id.strip(),
            'path': target_path,
            'page_count': len(pages),
            'pages': pages,
        }

    def execute_realtime(
        self,
        *,
        api_id: str,
        trnm: str,
        grp_no: str,
        refresh: str,
        items: list[str],
        types: list[str],
        timeout_seconds: int = 8,
        max_messages: int = 3,
    ) -> dict[str, Any]:
        """
        Register realtime stream via websocket and collect messages for a short window.
        """
        try:
            import websocket  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "websocket-client is required. Install with: python -m pip install websocket-client"
            ) from exc

        ws_url = self._build_ws_url(self._realtime_path)
        headers = [
            f"authorization: Bearer {self._get_token()}",
            f"api-id: {api_id.strip()}",
            "cont-yn: N",
            "next-key: ",
            "Content-Type: application/json;charset=UTF-8",
        ]
        payload = {
            "trnm": trnm,
            "grp_no": grp_no,
            "refresh": refresh,
            "data": [
                {
                    "item": items,
                    "type": types,
                }
            ],
        }

        messages: list[dict[str, Any]] = []
        ws = websocket.create_connection(ws_url, header=headers, timeout=timeout_seconds)
        try:
            ws.send(json.dumps(payload, ensure_ascii=False))
            deadline = time.time() + max(1, timeout_seconds)
            ws.settimeout(1)
            while time.time() < deadline and len(messages) < max(1, max_messages):
                try:
                    raw = ws.recv()
                except Exception:
                    continue
                if raw in (None, ""):
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw)
                    messages.append({"type": "json", "data": parsed})
                except Exception:
                    messages.append({"type": "text", "data": str(raw)})
        finally:
            try:
                ws.close()
            except Exception:
                pass

        return {
            "api_id": api_id.strip(),
            "ws_url": ws_url,
            "request": payload,
            "message_count": len(messages),
            "messages": messages,
        }

    def fetch_deposits(self, since: datetime) -> list[DepositRecord]:
        logger.info('입금 조회 시작 | since=%s', since.isoformat())
        rows = self._fetch_kt00015(tp='6', since=since)
        return [parsed for row in rows if (parsed := self._parse_cashflow(row, forced_direction='IN')) is not None]

    def fetch_withdrawals(self, since: datetime) -> list[DepositRecord]:
        logger.info('출금 조회 시작 | since=%s', since.isoformat())
        rows = self._fetch_kt00015(tp='7', since=since)
        return [parsed for row in rows if (parsed := self._parse_cashflow(row, forced_direction='OUT')) is not None]

    def fetch_trades(self, since: datetime) -> list[TradeRecord]:
        logger.info('매매 조회 시작 | since=%s', since.isoformat())
        rows = self._fetch_kt00015(tp='3', since=since)
        return [parsed for row in rows if (parsed := self._parse_trade(row)) is not None]

    def fetch_account_snapshot(self) -> AccountRecord | None:
        try:
            logger.info('계좌 스냅샷 API 요청 | api_id=%s', self._account_balance_api_id)
            headers = self._request_headers(api_id=self._account_balance_api_id)
            headers['cont-yn'] = 'N'
            headers['next-key'] = ''
            response = self._client.post(
                self._build_url(self._account_path),
                json={'qry_tp': '2'},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            self._check_return_code(payload)
            cash = self._to_float(self._pick(payload, 'entr', default='0'))
            logger.info(
                '계좌 스냅샷 API 응답 | status=%s return_code=%s',
                response.status_code,
                payload.get('return_code', 0),
            )
            logger.info('키움 계좌 스냅샷 조회 완료 | 예수금=%.0f', cash)
            return AccountRecord(
                account_no=self._account_no,
                cash_balance=abs(cash),
                occurred_at=datetime.now(tz=KST),
            )
        except Exception as exc:
            logger.warning('계좌 스냅샷 조회 실패: %s', exc)
            return None

    def _fetch_kt00015(self, *, tp: str, since: datetime) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        cont_yn = 'N'
        next_key = ''
        page_count = 0

        strt_dt = since.astimezone(KST).strftime('%Y%m%d')
        end_dt = datetime.now(tz=KST).strftime('%Y%m%d')

        while True:
            page_count += 1
            is_cashflow = tp in ('1', '6', '7', 'M')
            api_id = self._deposits_api_id if is_cashflow else self._trades_api_id
            headers = self._request_headers(api_id=api_id)
            headers['cont-yn'] = cont_yn
            headers['next-key'] = next_key

            body = {
                'strt_dt': strt_dt,
                'end_dt': end_dt,
                'tp': tp,
                'stk_cd': '',
                'crnc_cd': self._crnc_cd,
                'gds_tp': self._gds_tp,
                'frgn_stex_code': '',
                'dmst_stex_tp': self._dmst_stex_tp,
            }
            logger.info(
                '거래내역 API 요청 | api_id=%s tp=%s page=%s cont_yn=%s next_key=%s period=%s~%s',
                api_id,
                tp,
                page_count,
                cont_yn,
                'Y' if bool(next_key) else 'N',
                strt_dt,
                end_dt,
            )
            response = self._client.post(self._build_url(self._account_path), json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
            self._check_return_code(payload)

            rows = payload.get('trst_ovrl_trde_prps_array', [])
            if isinstance(rows, list):
                all_rows.extend([row for row in rows if isinstance(row, dict)])
            sample = self._summarize_row(rows[0]) if isinstance(rows, list) and rows else None
            logger.info(
                '거래내역 API 응답 | api_id=%s tp=%s status=%s rows=%s cont_yn=%s next_key=%s sample=%s',
                api_id,
                tp,
                response.status_code,
                len(rows) if isinstance(rows, list) else 0,
                response.headers.get('cont-yn', 'N'),
                'Y' if bool(response.headers.get('next-key', '')) else 'N',
                sample or '-',
            )

            cont_yn = response.headers.get('cont-yn', 'N')
            next_key = response.headers.get('next-key', '')
            if cont_yn != 'Y' or not next_key:
                break

        logger.info('키움 거래내역 조회 완료 | tp=%s 건수=%s 페이지=%s', tp, len(all_rows), page_count)
        return all_rows

    def _request_headers(self, *, api_id: str) -> dict[str, str]:
        return {
            'Content-Type': 'application/json;charset=UTF-8',
            'authorization': f'Bearer {self._get_token()}',
            'api-id': api_id,
        }

    def _get_token(self) -> str:
        now = datetime.now(tz=timezone.utc)
        if self._token and self._token_expires_at and now < self._token_expires_at - timedelta(seconds=60):
            return self._token

        response = self._client.post(
            self._build_url(self._token_path),
            json={
                'grant_type': 'client_credentials',
                'appkey': self._app_key,
                'secretkey': self._app_secret,
            },
            headers={'Content-Type': 'application/json;charset=UTF-8'},
        )
        response.raise_for_status()
        payload = response.json()
        self._check_return_code(payload)

        token = str(payload.get('token', '')).strip()
        if not token:
            raise ValueError('No token in Kiwoom token response')

        self._token = token
        expires_dt = str(payload.get('expires_dt', '')).strip()
        self._token_expires_at = self._parse_expires_dt(expires_dt)
        return token

    @staticmethod
    def _parse_expires_dt(value: str) -> datetime:
        if value:
            try:
                dt = datetime.strptime(value, '%Y%m%d%H%M%S')
                return dt.replace(tzinfo=KST).astimezone(timezone.utc)
            except ValueError:
                logger.warning('Unexpected expires_dt format: %s', value)
        return datetime.now(tz=timezone.utc) + timedelta(hours=1)

    @staticmethod
    def _check_return_code(payload: dict[str, Any]) -> None:
        code = payload.get('return_code')
        if code in (None, 0, '0'):
            return
        msg = payload.get('return_msg', '')
        raise RuntimeError(f'Kiwoom API error return_code={code} return_msg={msg}')

    def _parse_cashflow(self, row: dict[str, Any], *, forced_direction: str) -> DepositRecord | None:
        try:
            amount = self._to_float(self._pick(row, 'trde_amt', 'exct_amt', default='0'))
            trde_dt = str(self._pick(row, 'trde_dt'))
            proc_tm = str(self._pick(row, 'proc_tm', default='00:00:00'))
            occurred_at = self._parse_kst_datetime(trde_dt, proc_tm)

            deal_no = str(self._pick(row, 'trde_no', 'orig_deal_no', default=''))
            record_id = f'{trde_dt}-{deal_no or "NOID"}-{forced_direction}'

            return DepositRecord(
                id=record_id,
                account_no=self._account_no,
                amount=abs(amount),
                direction=forced_direction,
                occurred_at=occurred_at,
                description=str(self._pick(row, 'rmrk_nm', 'trde_kind_nm', default='')),
            )
        except Exception as exc:
            logger.warning(
                'Skip deposit row due to parse error: %s | row_keys=%s',
                exc,
                ','.join(sorted(row.keys())),
            )
            return None

    def _parse_trade(self, row: dict[str, Any]) -> TradeRecord | None:
        try:
            trde_dt = str(self._pick(row, 'trde_dt'))
            proc_tm = str(self._pick(row, 'proc_tm', default='00:00:00'))
            occurred_at = self._parse_kst_datetime(trde_dt, proc_tm)

            io_name = str(self._pick(row, 'io_tp_nm', 'rmrk_nm', default='')).strip()
            io_tp = str(self._pick(row, 'io_tp', default=''))
            side = self._to_side(io_name, io_tp)

            qty = self._to_float(self._pick(row, 'trde_qty_jwa_cnt', default='0'))
            price = self._to_float(self._pick(row, 'trde_unit', default='0'))
            trade_no = str(self._pick(row, 'trde_no', 'orig_deal_no', default=''))
            order_no = str(self._pick(row, 'ord_no', 'order_no', default=''))
            symbol = str(self._pick(row, 'stk_cd', default='')).replace('A', '', 1)
            gross_amount = self._to_float_or_none(
                self._pick(row, 'trde_amt', 'exct_amt', 'cntr_amt', default=None),
            )
            fee = self._to_float_or_none(self._pick(row, 'fee', 'fee_amt', 'cmsn', default=None))
            tax = self._to_float_or_none(self._pick(row, 'tax', 'tax_amt', default=None))
            net_amount = self._to_float_or_none(self._pick(row, 'stl_amt', 'net_amt', default=None))
            trade_kind = str(self._pick(row, 'trde_kind_nm', 'trde_tp_nm', default='')).strip()
            note = str(self._pick(row, 'rmrk_nm', default='')).strip()

            record_id = f'{trde_dt}-{trade_no or "NOID"}-{symbol}-{side}'
            return TradeRecord(
                id=record_id,
                account_no=self._account_no,
                symbol=symbol,
                side=side,
                quantity=abs(qty),
                price=abs(price),
                occurred_at=occurred_at,
                gross_amount=abs(gross_amount) if gross_amount is not None else None,
                fee=abs(fee) if fee is not None else None,
                tax=abs(tax) if tax is not None else None,
                net_amount=abs(net_amount) if net_amount is not None else None,
                order_no=order_no,
                trade_no=trade_no,
                trade_kind=trade_kind,
                io_type_code=io_tp,
                note=note,
            )
        except Exception as exc:
            logger.warning(
                'Skip trade row due to parse error: %s | row_keys=%s',
                exc,
                ','.join(sorted(row.keys())),
            )
            return None

    @staticmethod
    def _pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in data and data[key] not in (None, ''):
                return data[key]
        if default is not None:
            return default
        raise KeyError(f'Missing fields: {", ".join(keys)}')

    @staticmethod
    def _to_float(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).replace(',', '').strip()
        if not text:
            return 0.0
        return float(text)

    @staticmethod
    def _to_float_or_none(value: Any) -> float | None:
        if value in (None, ''):
            return None
        try:
            return KiwoomRestClient._to_float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_side(io_name: str, io_tp: str) -> str:
        if io_tp == '2' or 'BUY' in io_name.upper():
            return 'BUY'
        if io_tp == '1' or 'SELL' in io_name.upper():
            return 'SELL'
        return 'UNKNOWN'

    @staticmethod
    def _parse_kst_datetime(date_text: str, time_text: str) -> datetime:
        date_text = date_text.replace('-', '').strip()
        if len(date_text) != 8:
            raise ValueError(f'Unexpected date format: {date_text}')
        if ':' in time_text:
            parts = time_text.strip().split(':')
            hh = parts[0].zfill(2)
            mm = (parts[1] if len(parts) > 1 else '00').zfill(2)
            ss = (parts[2] if len(parts) > 2 else '00').zfill(2)
            compact = f'{hh}{mm}{ss}'
        else:
            compact = ''.join(ch for ch in time_text if ch.isdigit()).zfill(6)[:6]
        dt = datetime.strptime(f'{date_text}{compact}', '%Y%m%d%H%M%S')
        return dt.replace(tzinfo=KST)

    def _build_url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _build_ws_url(self, path: str) -> str:
        return f"{self._ws_base_url}/{path.lstrip('/')}"

    @staticmethod
    def _summarize_row(row: dict[str, Any]) -> str:
        date = str(row.get('trde_dt', ''))
        kind = str(row.get('io_tp_nm', '') or row.get('trde_kind_nm', ''))
        symbol = str(row.get('stk_cd', '')).replace('A', '', 1)
        amount = str(row.get('trde_amt', '') or row.get('exct_amt', ''))
        qty = str(row.get('trde_qty_jwa_cnt', ''))
        parts = [
            f'dt={date or "-"}',
            f'kind={kind or "-"}',
            f'symbol={symbol or "-"}',
            f'amount={amount or "-"}',
            f'qty={qty or "-"}',
        ]
        return ', '.join(parts)
