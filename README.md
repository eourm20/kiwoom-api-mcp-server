# 키움 MCP

카탈로그 + PDF 페이지 스펙 추출 기반으로 API를 자동 매핑/호출하는 MCP 서버입니다.

## 도구

- `kiwoom_auto_call`
  - 질문 또는 `api_id`를 받아 자동으로 스펙을 찾고 호출
  - `dry_run=true`면 실제 호출 없이 요청 계획만 반환
- `kiwoom_extract_api_spec`
  - 카탈로그 페이지 범위를 기준으로 PDF에서 Method/URL/필수 필드 추출
- `kiwoom_execute_api`
  - `api_id + body + path`로 직접 호출
- `kiwoom_execute_realtime`
  - 실시간 웹소켓 호출 (`0B` 등)
  - 예: `api_id=0B`, `item=005930`
- `kiwoom_catalog_get`
  - 코드 1건 조회
- `kiwoom_catalog_search`
  - 카탈로그 검색
- `kiwoom_catalog_recommend_for_question`
  - 질문 기반 후보 API 추천


## 환경변수

`kiwoom_mcp/.env.example` 참고

- `KIWOOM_CATALOG_PATH`
- `KIWOOM_API_PDF_PATH`
- `KIWOOM_BASE_URL`, `KIWOOM_APP_KEY`, `KIWOOM_APP_SECRET`, ...


## 실행

```powershell
python -m kiwoom_mcp.server
```

