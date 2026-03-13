# 키움 MCP

카탈로그 + PDF 페이지 스펙 추출 기반으로 API를 자동 매핑/호출하는 MCP 서버입니다.

## 프로젝트 구조

```text
.
├─ kiwoom_mcp/
│  ├─ server.py                      # MCP 서버 엔트리포인트, 도구 등록, 자동 호출 파이프라인
│  ├─ kiwoom_client.py               # 키움 REST/WebSocket 통신, 토큰 발급/재사용 처리
│  ├─ catalog_index.py               # 카탈로그 파싱/검색, API 코드별 페이지 범위 계산
│  ├─ pdf_spec_extractor.py          # PDF에서 Method/URL/필수 요청 필드 추출
│  ├─ models.py                      # 계좌/거래/입출금 데이터 모델 (자주 사용하는 것만 정형화해 필드 일관성 유지)
│  └─ .env.example                   # 환경변수 템플릿(샘플 값)
├─ docs/
│  ├─ KIWOOM_REST_API_CATALOG.md     # API 코드/카테고리/문서 페이지 매핑
│  └─ 키움 REST API 문서.pdf          # 실제 API 원문 스펙 PDF
└─ README.md                         # 실행/설정/사용 가이드
```

## 도구

| 도구 | 설명 |
|---|---|
| `kiwoom_auto_call` | 질문 또는 `api_id`를 받아 스펙 추출부터 실행까지 자동 처리 (`dry_run=true` 지원, `needs_input` 시 `required_field_guidance`/`required_input_message` 반환) |
| `kiwoom_extract_api_spec` | 카탈로그 페이지 범위를 기준으로 PDF에서 Method/URL/필수 필드 추출 |
| `kiwoom_execute_api` | `api_id + body + path`로 REST 직접 호출 |
| `kiwoom_execute_realtime` | 실시간 웹소켓 호출 (`0B` 등, 예: `api_id=0B`, `item=005930`) |
| `kiwoom_catalog_get` | 코드 1건 조회 |
| `kiwoom_catalog_search` | 카탈로그 검색 |
| `kiwoom_catalog_recommend_for_question` | 질문 기반 후보 API 추천 |


## 환경 변수 설정

`kiwoom_mcp/.env.example` 참고

- `KIWOOM_APP_KEY`: 키움 OpenAPI 앱 키
- `KIWOOM_APP_SECRET`: 앱 시크릿 키
- `KIWOOM_ACCOUNT_NO`: 조회/주문 대상 계좌번호
- `KIWOOM_BASE_URL`: 실전/모의투자 구분 url
- `KIWOOM_ALLOW_TRADE_EXECUTION`: `true`일 때만 주문/정정/취소 API 실행 허용 (기본: `false`)
- `KIWOOM_CATALOG_PATH`: API 카탈로그 문서 경로 (기본: `docs/KIWOOM_REST_API_CATALOG.md`)
- `KIWOOM_API_PDF_PATH`: 키움 API PDF 문서 경로 (기본: `docs/키움 REST API 문서.pdf`)


## 로컬 설치 및 MCP 등록

### 1. 저장소 클론 및 의존성 설치

```powershell
git clone https://github.com/eourm20/kiwoom-api-mcp-server.git
cd kiwoom-api-mcp-server

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 환경변수 설정

```powershell
Copy-Item kiwoom_mcp\.env.example kiwoom_mcp\.env
```

`kiwoom_mcp\.env`를 열어 아래 항목을 본인 값으로 변경합니다.

```env
KIWOOM_BASE_URL=https://api.kiwoom.com        # 실전: https://api.kiwoom.com / 모의: https://mockapi.kiwoom.com
KIWOOM_APP_KEY=your_app_key                   # 키움 OpenAPI 앱 키
KIWOOM_APP_SECRET=your_app_secret             # 키움 OpenAPI 앱 시크릿
KIWOOM_ACCOUNT_NO=your_account_no             # 조회/주문 대상 계좌번호
KIWOOM_ALLOW_TRADE_EXECUTION=false            # 주문/정정/취소 허용 여부 (기본 false, 실거래 시 true로 변경)
```

### 3. Claude Desktop 등록

Claude Desktop 설정 파일(`%APPDATA%\Claude\claude_desktop_config.json`)에 아래 내용을 추가합니다.
`<프로젝트 경로>`를 실제 클론한 경로로 변경하세요.

```json
{
  "mcpServers": {
    "kiwoom-mcp": {
      "command": "<프로젝트 경로>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kiwoom_mcp.server"],
      "cwd": "<프로젝트 경로>"
    }
  }
}
```

예시 (Windows):

```json
{
  "mcpServers": {
    "kiwoom-mcp": {
      "command": "C:\\Users\\yourname\\mcp-news-trade-coach\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kiwoom_mcp.server"],
      "cwd": "C:\\Users\\yourname\\mcp-news-trade-coach"
    }
  }
}
```

### 4. Claude 재시작

Claude Desktop을 재시작하면 `kiwoom_*` 도구가 활성화됩니다.

---

## 실행 (직접 실행 시)

```powershell
# stdio 기본 실행
python -m kiwoom_mcp.server
```

## MCP 사용 방법

1. `kiwoom_mcp/.env`에 개인 키/계좌/문서 경로를 설정
2. Claude Desktop 설정 파일에 MCP 서버 등록 (위 **로컬 설치 및 MCP 등록** 참고)
3. Claude 재시작 후 `kiwoom_*` 도구 호출
4. 응답이 `needs_input`이면 `required_field_guidance`를 확인해 필수값을 채워 재호출

### 예시 호출

- `kiwoom_catalog_search`: 카탈로그 검색
- `kiwoom_extract_api_spec`: API 스펙 확인
- `kiwoom_auto_call`: 자동 실행 (`dry_run=true` 권장 후 실제 실행)
- 자연어 예시: `내 예수금 조회해줘`
- 자연어 예시: `1년간 내 투자 내역 알려줘`

### 참고

- `종목/기간/구분(입금, 출금, 미체결)`을 같이 쓰면 매핑 정확도가 올라갑니다.
- 필수값이 부족하면 `needs_input`으로 필요한 값과 옵션을 안내합니다.
- 주문/정정/취소 API는 기본 차단입니다. 실행하려면 `KIWOOM_ALLOW_TRADE_EXECUTION=true`와 함께 `approve_trade=true`, `approval_note`를 모두 제공해야 합니다.
