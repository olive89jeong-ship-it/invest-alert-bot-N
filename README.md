# Invest Alert Bot

전력망·AI 전력 인프라·인프라 ETF/종목을 매일 점검해서 텔레그램으로 보내는 GitHub Actions용 봇입니다.

## 현재 구조

```text
GitHub Actions
→ universe.csv 관심 ETF/종목 읽기
→ Yahoo Finance 비공식 차트 API로 가격 확인
→ Google News RSS로 관련 뉴스 수집
→ OpenAI API로 요약
→ Telegram으로 알림 발송
```

## 이미 GitHub Secrets에 들어가 있어야 하는 값

현재 화면 기준으로 아래 3개가 들어가 있으면 됩니다.

```text
OPENAI_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

## 업로드 방법

이 폴더 안의 파일을 GitHub 레포 최상단에 그대로 업로드하세요.

최종 구조는 아래처럼 되어야 합니다.

```text
invest-alert-bot/
├─ .github/
│  └─ workflows/
│     └─ invest-alert.yml
├─ main.py
├─ universe.csv
├─ requirements.txt
├─ README.md
├─ .gitignore
└─ .env.example
```

## 수동 실행 방법

GitHub 레포에서:

```text
Actions
→ Invest Alert Bot
→ Run workflow
```

## 알림 시간

기본 설정은 한국시간 기준 대략 월~토 오전 8시입니다.

GitHub Actions의 cron은 UTC 기준이라서 아래처럼 설정했습니다.

```yaml
cron: "0 23 * * 0-5"
```

## 관심종목 수정

`universe.csv`에서 종목을 추가하거나 삭제하면 됩니다.

미국 종목은 예:

```csv
GRID,First Trust NASDAQ Clean Edge Smart Grid Infrastructure ETF,ETF,스마트그리드,smart grid infrastructure ETF power grid,1
```

국내상장 ETF는 Yahoo Finance 티커 형식으로 `.KS`를 붙였습니다.

```csv
486450.KS,SOL 미국AI전력인프라,KR ETF,AI전력인프라,SOL 미국AI전력인프라 AI 전력 인프라 ETF,1
```

## 주의

이 봇은 투자 판단 보조용입니다. 자동매수 기능은 없습니다.
한국투자증권 API도 사용하지 않습니다.
