# Kiwoom API MCP Server

이 프로젝트는 **Kiwoom OpenAPI(REST/WebSocket)** 를 MCP 도구로 노출하여, LLM/에이전트가 다음 작업을 자동화할 수 있게 합니다.

- 질문 기반 API 후보 탐색
- 카탈로그 + PDF 명세 기반 요청 스펙 추출
- REST/실시간 API 실행
- 거래성 API 실행 안전장치(이중 승인) 적용

본 README는 저장소의 실제 코드(특히 `kiwoom_mcp/server.py`, `kiwoom_mcp/kiwoom_client.py`) 기준으로 작성되었습니다.

---

## 1. 프로젝트 개요 (무엇을 자동화하는지)

자동화 대상은 아래 파이프라인입니다.

1. 자연어 질문을 API 후보로 매핑 (`kiwoom_catalog_recommend_for_question`)
2. API ID 기준으로 PDF 명세 추출 (`kiwoom_extract_api_spec`)
3. 필수 Body/Header 정보를 기반으로 요청 바디 자동 구성 (`kiwoom_auto_call` 내부 `_build_auto_body`)
4. REST 또는 WebSocket 경로로 실제 실행 (`kiwoom_execute_api`, `kiwoom_execute_realtime`)
5. 거래 API는 기본 차단 후 명시적 승인 시에만 실행

---

## 2. 기술 스택/프레임워크

`requirements.txt` 기준:

- `mcp==1.26.0` (MCP 서버 프레임워크, `FastMCP`)
- `python-dotenv==1.0.1` (`.env` 로딩)
- `httpx==0.28.1` (REST 호출)
- `websocket-client==1.8.0` (실시간 WebSocket)
- `pymupdf==1.26.5` (PDF 명세 추출)
- `pydantic==2.12.5` (도메인 모델)
- `tzdata==2025.2` (타임존 데이터)

언어/런타임:

- Python 3.x (로컬 `.venv` 사용 전제)

---

## 3. 아키텍처 다이어그램(텍스트 다이어그램)

```text
[MCP Client (e.g. Claude Desktop)]
                |
                v
     kiwoom_mcp/server.py (FastMCP tools)
                |
                |-- catalog_index.py
                |     |- load_catalog()
                |     |- search_catalog()
                |     |- page_range_for_code()
                |
                |-- pdf_spec_extractor.py
                |     |- extract_api_spec_from_pdf()
                |
                |-- kiwoom_client.py
                      |- _get_token() -> POST /oauth2/token
                      |- execute_api() -> POST {account_path or path}
                      |- execute_realtime() -> WS {ws_base_url + realtime_path}
```

참고: DB/큐/배치 워커 계층은 현재 코드에 존재하지 않습니다.

---

## 4. 디렉터리 구조(현재 코드 기준)

```text
.
├─ README.md
├─ requirements.txt
├─ docs/
│  ├─ KIWOOM_REST_API_CATALOG.md
│  └─ (Kiwoom REST API PDF 파일)
└─ kiwoom_mcp/
   ├─ .env.example
   ├─ API_Mapping_Method.md
   ├─ KIWOOM_REST_API_CATALOG.md
   ├─ server.py
   ├─ kiwoom_client.py
   ├─ catalog_index.py
   ├─ pdf_spec_extractor.py
   └─ models.py
```

---

## 5. 실행 프로세스 상세 (시작 -> 처리 -> 저장 -> 후처리)

### 5.1 시작 단계

- 엔트리포인트: `python -m kiwoom_mcp.server`
- `server.py` import 시 `_load_env_files()` 실행
  - 우선순위:
    1. `kiwoom_mcp/.env`
    2. 프로젝트 루트 `.env` (fallback)
- `FastMCP` 인스턴스 생성 (`mcp = FastMCP(...)`)

### 5.2 처리 단계

- MCP 툴 호출 진입
- `_build_client()`로 `KiwoomRestClient` 생성
- 필요 시 `_get_token()`으로 OAuth 토큰 발급/캐시
- REST면 `execute_api()`, 실시간이면 `execute_realtime()`

### 5.3 저장 단계

- **영구 저장 없음**
- 응답은 모두 함수 반환값(dict/Pydantic)으로만 전달
- 파일/DB에 결과를 적재하는 코드 없음

### 5.4 후처리 단계

- `server.py` 각 툴에서 `finally: client.close()`로 세션 정리
- 에러는 예외 또는 `{ok: False, ...}` 형태로 반환

---

## 6. 핵심 모듈 책임 분리 (파일 단위)

### `kiwoom_mcp/server.py`

- MCP 툴 등록 및 orchestration
- 환경변수 검증: `_env()`
- 경로 해석: `_resolve_configured_path()`, `_catalog_path()`, `_default_pdf_path()`
- 거래 실행 보호:
  - `_is_trade_api()`
  - `_trade_execution_globally_allowed()`
  - `_trade_approval_response()`
- 자동 호출 파이프라인: `kiwoom_auto_call()`

등록 툴:

1. `kiwoom_execute_api`
2. `kiwoom_execute_realtime`
3. `kiwoom_catalog_get`
4. `kiwoom_catalog_search`
5. `kiwoom_catalog_recommend_for_question`
6. `kiwoom_extract_api_spec`
7. `kiwoom_auto_call`

### `kiwoom_mcp/kiwoom_client.py`

- Kiwoom REST/WebSocket 저수준 클라이언트
- 토큰 발급/만료 관리 (`_get_token`, `_parse_expires_dt`)
- REST 연속조회 (`execute_api` + `cont-yn`/`next-key`)
- WebSocket 실시간 등록/수신 (`execute_realtime`)
- 응답 파싱 유틸 (`_parse_trade`, `_parse_cashflow` 등)

### `kiwoom_mcp/catalog_index.py`

- 카탈로그 마크다운 파싱 (`load_catalog`)
- 검색/코드 조회 (`search_catalog`, `find_by_code`)
- PDF 원본 경로 파싱 (`get_catalog_source_pdf`)
- API별 페이지 범위 계산 (`page_range_for_code`)

### `kiwoom_mcp/pdf_spec_extractor.py`

- PDF 텍스트에서 Method/URL 추출
- 필수 Header/Body 필드 추출
- 결과 모델: `ApiSpec`

### `kiwoom_mcp/models.py`

- 도메인 모델 정의:
  - `DepositRecord`
  - `TradeRecord`
  - `AccountRecord`

---

## 7. 스케줄러/배치 잡 설명 (주기, 목적, 의존성)

현재 코드 기준:

- 스케줄러/크론/잡 등록 코드: **없음**
- 백그라운드 배치 루프: **없음**
- 메시지 큐 소비자/생산자: **없음**

근거:

- `kiwoom_mcp/*.py` 내 정기 실행 루프/스케줄러 import 미존재
- 엔트리포인트는 MCP 서버 구동만 수행

---

## 8. 데이터 저장 구조 (핵심 테이블/엔티티 요약)

### 영구 저장소

- RDB/NoSQL/파일 적재 로직: **없음**
- 마이그레이션/DDL 파일: **없음**

### 런타임 엔티티(메모리/응답 객체)

- `DepositRecord` (`models.py`)
- `TradeRecord` (`models.py`)
- `AccountRecord` (`models.py`)
- `CatalogEntry` (`catalog_index.py`, dataclass)
- `ApiSpec` (`pdf_spec_extractor.py`, dataclass)

---

## 9. Data Dictionary (테이블별 주요 컬럼: 타입/설명/예시)

본 저장소에는 DB 테이블이 없어, **엔티티 필드 기준 Data Dictionary** 로 대체합니다.

### 9.1 `DepositRecord` (`kiwoom_mcp/models.py`)

| 필드 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `id` | `str` | 입출금 레코드 식별자 | `20260317-12345-IN` |
| `account_no` | `str` | 계좌번호 | `12345678` |
| `amount` | `float` | 금액(절대값) | `1500000.0` |
| `direction` | `str` | `IN` 또는 `OUT` | `IN` |
| `occurred_at` | `datetime` | 발생 시각(KST 기반 파싱) | `2026-03-17T09:10:11+09:00` |
| `description` | `str` | 비고/거래종류명 | `입금` |

### 9.2 `TradeRecord` (`kiwoom_mcp/models.py`)

| 필드 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `id` | `str` | 체결 레코드 식별자 | `20260317-98765-005930-BUY` |
| `account_no` | `str` | 계좌번호 | `12345678` |
| `symbol` | `str` | 종목코드(`A` prefix 제거) | `005930` |
| `side` | `str` | `BUY`/`SELL`/`UNKNOWN` | `BUY` |
| `quantity` | `float` | 수량(절대값) | `10.0` |
| `price` | `float` | 단가(절대값) | `72000.0` |
| `occurred_at` | `datetime` | 체결 시각 | `2026-03-17T09:01:02+09:00` |
| `gross_amount` | `float \| None` | 체결금액 | `720000.0` |
| `fee` | `float \| None` | 수수료 | `120.0` |
| `tax` | `float \| None` | 세금 | `0.0` |
| `net_amount` | `float \| None` | 정산금액 | `719880.0` |
| `order_no` | `str` | 주문번호 | `1234567890` |
| `trade_no` | `str` | 거래번호 | `98765` |
| `trade_kind` | `str` | 거래유형명 | `현금매수` |
| `io_type_code` | `str` | 매수/매도 코드 | `2` |
| `note` | `str` | 비고 | `정상체결` |

### 9.3 `AccountRecord` (`kiwoom_mcp/models.py`)

| 필드 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `account_no` | `str` | 계좌번호 | `12345678` |
| `cash_balance` | `float` | 예수금(절대값) | `3150000.0` |
| `occurred_at` | `datetime` | 스냅샷 시각 | `2026-03-17T09:15:00+09:00` |

---

## 10. 외부 연동 포인트 (API/웹훅/봇/메시지 등)

### 10.1 Kiwoom REST

- Token 발급:
  - `POST {KIWOOM_BASE_URL}{KIWOOM_TOKEN_PATH}`
  - 기본 `KIWOOM_TOKEN_PATH=/oauth2/token`
- 계좌/거래 API:
  - `POST {KIWOOM_BASE_URL}{path}`
  - 기본 path는 `KIWOOM_ACCOUNT_PATH=/api/dostk/acnt`

### 10.2 Kiwoom WebSocket

- 실시간 등록:
  - `WS {KIWOOM_WS_BASE_URL}{KIWOOM_REALTIME_PATH}`
  - 기본 `wss://api.kiwoom.com:10000/api/dostk/websocket`

### 10.3 기타 연동

- 웹훅, Kafka, RabbitMQ, Redis Queue, DB 연결: **현재 코드에 없음**

---

## 11. 설정 가이드 (.env, yaml/json 설정 항목)

### 11.1 `.env` (`kiwoom_mcp/.env.example` 기준)

필수값:

- `KIWOOM_APP_KEY`
- `KIWOOM_APP_SECRET`
- `KIWOOM_ACCOUNT_NO`
- `KIWOOM_BASE_URL`

주요 선택값:

- `KIWOOM_ALLOW_TRADE_EXECUTION` (기본 `false`)
- `KIWOOM_TOKEN_PATH` (기본 `/oauth2/token`)
- `KIWOOM_ACCOUNT_PATH` (기본 `/api/dostk/acnt`)
- `KIWOOM_WS_BASE_URL` (기본 `wss://api.kiwoom.com:10000`)
- `KIWOOM_REALTIME_PATH` (기본 `/api/dostk/websocket`)
- `KIWOOM_CATALOG_PATH` (기본 `docs/KIWOOM_REST_API_CATALOG.md`)
- `KIWOOM_API_PDF_PATH` (기본 `docs/키움 REST API 문서.pdf`)
- `MCP_TRANSPORT` (`stdio`/`http`/`sse`)
- `MCP_HOST`, `MCP_PORT`, `MCP_HTTP_PATH`

계좌/거래 조회 API ID 및 조회 파라미터 (기본값 유지 권장):

- `KIWOOM_DEPOSITS_API_ID` (기본 `kt00015`)
- `KIWOOM_TRADES_API_ID` (기본 `kt00015`)
- `KIWOOM_ACCOUNT_BALANCE_API_ID` (기본 `kt00001`)
- `KIWOOM_DMST_STEX_TP` (국내거래소구분, 기본 `%`)
- `KIWOOM_GDS_TP` (상품구분, 기본 `1`)
- `KIWOOM_CRNC_CD` (통화코드, 기본 `KRW`)

### 11.2 Claude Desktop JSON 설정

필수 항목(예시):

```json
{
  "mcpServers": {
    "kiwoom-mcp": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kiwoom_mcp.server"],
      "env": {
        "PYTHONPATH": "C:\\path\\to\\kiwoom-api-mcp-server",
        "MCP_TRANSPORT": "stdio",
        "KIWOOM_BASE_URL": "https://api.kiwoom.com",
        "KIWOOM_ALLOW_TRADE_EXECUTION": "false"
      }
    }
  }
}
```

---

## 12. 실행 방법 (로컬/테스트/운영)

### 12.1 로컬 실행

```powershell
git clone https://github.com/eourm20/kiwoom-api-mcp-server.git
cd kiwoom-api-mcp-server
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item kiwoom_mcp\.env.example kiwoom_mcp\.env
python -m kiwoom_mcp.server
```

### 12.2 테스트

- 저장소 내 자동 테스트 스위트(`tests/`, `pytest` 설정) **확인되지 않음**
- 현재는 MCP 클라이언트에서 툴 직접 호출 방식으로 검증

### 12.3 운영

- `MCP_TRANSPORT=stdio`: Claude Desktop 임베디드 구동
- `MCP_TRANSPORT=http` 또는 `sse`: 서버 모드 구동
- 운영 전 반드시 `KIWOOM_ALLOW_TRADE_EXECUTION=false` 확인

---

## 13. 운영 체크리스트 (장애 포인트, 로그 위치, 레이트리밋, 안전장치)

### 13.1 장애 포인트

- 필수 env 누락: `_env()`에서 즉시 `RuntimeError`
- 토큰 응답 오류: `_check_return_code()` 예외
- PDF 의존성 누락: `pymupdf(fitz)` import 실패
- 실시간 의존성 누락: `websocket-client` import 실패

### 13.2 로그

- `kiwoom_client.py`는 `logging.getLogger(__name__)` 사용
- 기본 로그 핸들러/파일 경로 설정 코드는 저장소 내 **없음**
- 실행 환경(호스트 애플리케이션)에서 로깅 설정 필요

### 13.3 레이트리밋

- 레이트리밋 제어/재시도(backoff) 명시 구현: **확인되지 않음**
- Kiwoom API 정책은 별도 운영 문서에서 확인 필요

### 13.4 안전장치

거래성 API 보호 조건:

1. 환경변수 `KIWOOM_ALLOW_TRADE_EXECUTION=true`
2. 툴 인자 `approve_trade=true`
3. 툴 인자 `approval_note` 비어있지 않음

조건 미충족 시 실제 호출 대신 `mode=approval_required` 응답 반환.

---

## 14. 변경 시 유지보수 규칙 (문서 동기화 원칙)

아래 변경이 발생하면 README 동기화가 필수입니다.

1. `server.py` MCP 툴 시그니처 변경
2. `kiwoom_client.py` 토큰/호출/응답 처리 로직 변경
3. `.env.example` 키 추가/삭제/기본값 변경
4. 카탈로그 경로/파서 규칙(`catalog_index.py`) 변경
5. PDF 스펙 추출 규칙(`pdf_spec_extractor.py`) 변경
6. 거래 보호 정책(`_is_trade_api`, 승인 조건) 변경

권장 절차:

1. 코드 PR 작성
2. README 섹션 5~13 동시 수정
3. “문서와 코드 불일치 가능 포인트” 섹션 재검토

---

## 문서와 코드 불일치 가능 포인트

1. 한글 문자열 인코딩
- 일부 소스/문서에서 콘솔 인코딩 이슈가 보이며(터미널 출력 깨짐), `server.py`의 거래 키워드 매칭 문자열이 런타임 인코딩 환경에 따라 기대와 다르게 동작할 수 있습니다.

2. 카탈로그/실제 API 명세 동기화
- `kiwoom_auto_call`은 카탈로그 + PDF 추출 결과에 의존하므로, `KIWOOM_REST_API_CATALOG.md` 또는 PDF 버전이 바뀌면 자동 생성 필드 정확도가 달라질 수 있습니다.

3. 테스트 자동화 부재
- 저장소 내 공식 테스트 코드가 확인되지 않아, 동작 보장은 수동 검증에 의존합니다.

4. 레이트리밋 정책 반영 여부
- 현재 코드에 명시적 레이트리밋 제어가 없으므로, 실제 운영 한도와 문서 내용이 어긋날 가능성이 있습니다. (운영 환경에서 별도 보완 필요)

5. 데이터 저장 요구사항
- 현재는 영구 저장이 없지만, 향후 DB가 추가되면 섹션 8/9는 즉시 개편되어야 합니다.
