import csv
import html
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

UNIVERSE_FILE = "universe.csv"


@dataclass
class Asset:
    ticker: str
    name: str
    category: str
    theme: str
    news_query: str
    enabled: bool


def read_universe(path: str = UNIVERSE_FILE) -> list[Asset]:
    assets: list[Asset] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            enabled = str(row.get("enabled", "1")).strip() == "1"
            if not enabled:
                continue
            assets.append(
                Asset(
                    ticker=row["ticker"].strip(),
                    name=row["name"].strip(),
                    category=row["category"].strip(),
                    theme=row["theme"].strip(),
                    news_query=row["news_query"].strip(),
                    enabled=enabled,
                )
            )
    return assets


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct_change(now: float | None, before: float | None) -> float | None:
    if now is None or before in (None, 0):
        return None
    return (now / before - 1) * 100


def get_yahoo_chart(ticker: str, range_: str = "1mo", interval: str = "1d") -> dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(ticker)}"
    params = {
        "range": range_,
        "interval": interval,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 invest-alert-bot"
    }

    response = requests.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()

    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo chart result가 비어 있습니다: {ticker}")

    result0 = result[0]
    meta = result0.get("meta", {})
    quote = result0.get("indicators", {}).get("quote", [{}])[0]

    closes = [safe_float(x) for x in quote.get("close", [])]
    volumes = [safe_float(x) for x in quote.get("volume", [])]
    timestamps = result0.get("timestamp", [])

    rows = []
    for i, close in enumerate(closes):
        if close is None:
            continue
        rows.append({
            "timestamp": timestamps[i] if i < len(timestamps) else None,
            "close": close,
            "volume": volumes[i] if i < len(volumes) else None,
        })

    if not rows:
        raise RuntimeError(f"종가 데이터가 없습니다: {ticker}")

    last = rows[-1]["close"]
    prev = rows[-2]["close"] if len(rows) >= 2 else safe_float(meta.get("previousClose"))
    close_5d_ago = rows[-6]["close"] if len(rows) >= 6 else None
    close_20d_ago = rows[-21]["close"] if len(rows) >= 21 else None

    return {
        "ticker": ticker,
        "currency": meta.get("currency", ""),
        "exchange": meta.get("exchangeName", ""),
        "regular_market_price": safe_float(meta.get("regularMarketPrice")) or last,
        "last_close": last,
        "prev_close": prev,
        "ret_1d": pct_change(last, prev),
        "ret_5d": pct_change(last, close_5d_ago),
        "ret_20d": pct_change(last, close_20d_ago),
        "volume": rows[-1]["volume"],
        "data_points": len(rows),
    }


def format_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def format_num(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value:,.0f}"
    return f"{value:.2f}"


def make_signal(quote: dict[str, Any]) -> str:
    ret_1d = quote.get("ret_1d")
    ret_5d = quote.get("ret_5d")
    ret_20d = quote.get("ret_20d")

    if ret_20d is not None and ret_20d >= 8:
        return "20일 강한 상승 추세"
    if ret_5d is not None and ret_5d >= 4:
        return "최근 5거래일 강세"
    if ret_1d is not None and ret_1d <= -3:
        return "단기 급락, 변동성 확인"
    if ret_20d is not None and ret_20d <= -8:
        return "20일 약세 추세"
    return "관찰"


def fetch_google_news(query: str, max_items: int = 5) -> list[dict[str, str]]:
    encoded = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(url)

    items = []
    for entry in feed.entries[:max_items]:
        items.append({
            "title": getattr(entry, "title", ""),
            "link": getattr(entry, "link", ""),
            "published": getattr(entry, "published", ""),
        })
    return items


def collect_news(assets: list[Asset]) -> list[dict[str, str]]:
    base_queries = [
        "전력망 AI 전력 인프라 ETF",
        "power grid infrastructure ETF AI electricity demand",
        "smart grid infrastructure investment",
        "data center power demand grid investment",
    ]

    for asset in assets[:6]:
        if asset.news_query:
            base_queries.append(asset.news_query)

    seen = set()
    news: list[dict[str, str]] = []

    for query in base_queries:
        try:
            items = fetch_google_news(query, max_items=4)
            for item in items:
                title_key = item["title"].strip().lower()
                if not title_key or title_key in seen:
                    continue
                seen.add(title_key)
                item["query"] = query
                news.append(item)
                if len(news) >= 14:
                    return news
            time.sleep(0.2)
        except Exception as e:
            print(f"News fetch failed: {query} / {e}")

    return news


def build_price_section(assets: list[Asset], quotes: dict[str, dict[str, Any]], errors: dict[str, str]) -> str:
    lines = ["<b>📊 전력망·AI전력 ETF/종목 체크</b>"]

    for asset in assets:
        q = quotes.get(asset.ticker)
        if not q:
            err = errors.get(asset.ticker, "조회 실패")
            lines.append(f"• <b>{html.escape(asset.ticker)}</b> {html.escape(asset.name)}: 조회 실패 ({html.escape(err[:80])})")
            continue

        price = q.get("last_close")
        currency = q.get("currency") or ""
        signal = make_signal(q)

        lines.append(
            "• "
            f"<b>{html.escape(asset.ticker)}</b> "
            f"{html.escape(asset.name)} | "
            f"{price:.2f} {html.escape(currency)} | "
            f"1D {format_pct(q.get('ret_1d'))}, "
            f"5D {format_pct(q.get('ret_5d'))}, "
            f"20D {format_pct(q.get('ret_20d'))} | "
            f"{html.escape(signal)}"
        )

    return "\n".join(lines)


def build_news_text(news: list[dict[str, str]], limit: int = 8) -> str:
    if not news:
        return "관련 뉴스 없음"

    lines = []
    for idx, item in enumerate(news[:limit], start=1):
        title = html.escape(item.get("title", ""))
        link = item.get("link", "")
        if link:
            lines.append(f"{idx}. <a href=\"{html.escape(link)}\">{title}</a>")
        else:
            lines.append(f"{idx}. {title}")
    return "\n".join(lines)


def call_openai_summary(assets: list[Asset], quotes: dict[str, dict[str, Any]], news: list[dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY가 없어 AI 요약은 생략했습니다."

    price_lines = []
    for asset in assets:
        q = quotes.get(asset.ticker)
        if not q:
            continue
        price_lines.append(
            f"{asset.ticker} {asset.name}: "
            f"1D {format_pct(q.get('ret_1d'))}, "
            f"5D {format_pct(q.get('ret_5d'))}, "
            f"20D {format_pct(q.get('ret_20d'))}, "
            f"signal={make_signal(q)}"
        )

    news_lines = [f"- {item.get('title', '')}" for item in news[:12]]

    prompt = f"""
너는 한국어 투자 모니터링 보조자다.
아래 ETF/종목 가격 흐름과 뉴스 제목을 바탕으로 텔레그램 알림용 요약을 작성해라.

조건:
- 투자 추천이 아니라 모니터링 요약으로 작성
- 과장 금지
- 모르면 모른다고 표시
- 5줄 이내
- 핵심 테마, 강한 종목, 주의할 점을 포함
- 한국어로 작성

[가격 흐름]
{chr(10).join(price_lines)}

[뉴스 제목]
{chr(10).join(news_lines)}
""".strip()

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You write concise Korean investment monitoring summaries."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=40)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"AI 요약 실패: {e}"


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN이 없습니다.")
    if not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID가 없습니다.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in split_message(text):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
        time.sleep(0.5)


def main() -> None:
    assets = read_universe()
    quotes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    for asset in assets:
        try:
            quotes[asset.ticker] = get_yahoo_chart(asset.ticker)
        except Exception as e:
            errors[asset.ticker] = str(e)

    news = collect_news(assets)
    price_section = build_price_section(assets, quotes, errors)
    ai_summary = call_openai_summary(assets, quotes, news)
    news_section = build_news_text(news, limit=8)

    now_kst = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    message = "\n\n".join([
        f"<b>🤖 투자 테마 알림</b>\n실행시각: {html.escape(now_kst)}",
        price_section,
        f"<b>🧠 AI 요약</b>\n{html.escape(ai_summary)}",
        f"<b>📰 관련 뉴스</b>\n{news_section}",
        "※ 자동매수 없음. 투자 판단 보조용 알림입니다.",
    ])

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
