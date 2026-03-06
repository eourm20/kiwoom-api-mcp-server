📘 Kiwoom MCP API Mapping Guide (Markdown)
# Kiwoom REST API MCP Mapping Guide

본 문서는 자연어 질문을 Kiwoom REST API로 자동 매핑하기 위한 MCP(Multi-Command Processor)용 스펙이다.

---

## 1. MCP 목적

- 자연어 질문 → Intent 분류
- Intent → API ID 매핑
- API ID → 실제 호출
- 결과 → GPT가 해석 및 분석

---

## 2. 전체 흐름

User Question  
→ Intent Classification  
→ API Mapping  
→ Parameter Extraction  
→ API Call  
→ Post Processing  
→ Answer

---

## 3. 인증 (OAuth)

### au10001 접근토큰 발급
POST /oauth2/token

```json
{
  "grant_type": "client_credentials",
  "appkey": "APP_KEY",
  "secretkey": "SECRET_KEY"
}

au10002 접근토큰 폐기

POST /oauth2/revoke

4. 계좌 / 손익 API
API ID	이름	설명
ka00001	계좌번호조회	계좌 조회
ka01690	일별잔고수익률	일별 수익률
ka10072	일자별 종목별 실현손익	날짜 기준
ka10073	일자별 종목별 실현손익(기간)	기간 기준
ka10074	일자별 실현손익	전체 손익
ka10075	미체결요청	미체결 주문
ka10076	체결요청	체결 내역
ka10077	당일실현손익상세	오늘 손익
5. 종목 정보 / 차트
API ID	이름	설명
ka10001	주식기본정보	종목 기본
ka10080	주식분봉차트	분봉
ka10081	주식일봉차트	일봉
ka10082	주식주봉차트	주봉
ka10083	주식월봉차트	월봉
ka10094	주식년봉차트	년봉
6. 주문 API
API ID	이름
kt10000	주식매수
kt10001	주식매도
kt10002	주문정정
kt10003	주문취소
7. Intent 정의
Intent	의미
ACCOUNT_INFO	계좌조회
REALIZED_PNL	실현손익
HOLDING_STATUS	보유잔고
STOCK_INFO	종목정보
STOCK_CHART	차트조회
ORDER_BUY	매수
ORDER_SELL	매도
8. Intent → API 매핑
Intent	API ID
ACCOUNT_INFO	ka00001
REALIZED_PNL	ka10074
REALIZED_PNL_PERIOD	ka10073
STOCK_INFO	ka10001
STOCK_CHART_DAILY	ka10081
STOCK_CHART_MONTH	ka10083
ORDER_BUY	kt10000
ORDER_SELL	kt10001
9. MCP 출력 예시
{
  "intent": "STOCK_CHART_MONTH",
  "api_id": "ka10083",
  "params": {
    "stk_cd": "005930"
  }
}

10. 예시 질문 처리

Q: 삼성전자 한달 차트 보여줘
→ Intent: STOCK_CHART_MONTH
→ API: ka10083
→ stk_cd = 005930

Q: 내 30일간 거래 손익 알려줘
→ Intent: REALIZED_PNL_PERIOD
→ API: ka10073

11. 확장 방식

신규 API 추가 시 테이블만 확장

Intent 추가 가능

JSON Schema 유지