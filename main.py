import csv
import html
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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
    theme_id: str
    theme_name: str
    ticker: str
    name: str
    category: str
    role: str
    plain_explain: str
    news_query: str
    enabled: bool


def read_universe(path: str = UNIVERSE_FILE) -> list[Asset]:
    assets: list[Asset] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if str(row.get("enabled", "1")).strip() != "1":
                continue
            assets.append(
                Asset(
                    theme_id=row["theme_id"].strip(),
                    theme_name=row["theme_name"].strip(),
                    ticker=row["ticker"].strip(),
                    name=row["name"].strip(),
                    category=row["category"].strip(),
                    role=row["role"].strip(),
                    plain_explain=row["plain_explain"].strip(),
                    news_query=row["news_query"].strip(),
                    enabled=True,
                )
            )
    return assets


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pct_change(now: float | None, before: float | None) -> float | None:
    if now is None or before in (None, 0):
        return None
    return (now / before - 1) * 100


def get_yahoo_chart(ticker: str, range_: str = "1mo", interval: str = "1d") -> dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(ticker)}"
    params = {"range": range_, "interval": interval}
    headers = {"User-Agent": "Mozilla/5.0 invest-alert-bot-theme-radar"}

    response = requests.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()

    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"가격 데이터 없음: {ticker}")

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
        raise RuntimeError(f"종가 없음: {ticker}")

    last = rows[-1]["close"]
    prev = rows[-2]["close"] if len(rows) >= 2 else safe_float(meta.get("previousClose"))
    close_5d_ago = rows[-6]["close"] if len(rows) >= 6 else None
    close_20d_ago = rows[-21]["close"] if len(rows) >= 21 else None

    return {
        "ticker": ticker,
        "currency": meta.get("currency", ""),
        "exchange": meta.get("exchangeName", ""),
        "price": last,
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


def format_price(value: float | None, currency: str = "") -> str:
    if value is None:
        return "N/A"
    if currency == "KRW":
        return f"{value:,.0f}원"
    return f"{value:.2f} {currency}".strip()


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
            "query": query,
        })
    return items


def collect_news_by_theme(assets: list[Asset]) -> dict[str, list[dict[str, str]]]:
    theme_queries: dict[str, set[str]] = {}
    for asset in assets:
        theme_queries.setdefault(asset.theme_id, set()).add(asset.news_query)
        theme_queries[asset.theme_id].add(asset.theme_name)

    common_queries = [
        "AI 전력 인프라 데이터센터 전력망 투자",
        "power grid investment data center AI electricity demand",
        "smart grid infrastructure ETF",
        "전력설비 ETF AI 전력",
    ]

    news_by_theme: dict[str, list[dict[str, str]]] = {}
    seen_global = set()

    for theme_id, queries in theme_queries.items():
        news_by_theme[theme_id] = []
        for query in list(queries)[:3] + common_queries[:1]:
            try:
                for item in fetch_google_news(query, max_items=4):
                    key = item["title"].strip().lower()
                    if not key or key in seen_global:
                        continue
                    seen_global.add(key)
                    news_by_theme[theme_id].append(item)
                    if len(news_by_theme[theme_id]) >= 5:
                        break
                time.sleep(0.2)
            except Exception as e:
                print(f"뉴스 수집 실패: {query} / {e}")
            if len(news_by_theme[theme_id]) >= 5:
                break

    return news_by_theme


def theme_score(theme_assets: list[Asset], quotes: dict[str, dict[str, Any]], news_count: int) -> dict[str, Any]:
    rets_1d = []
    rets_5d = []
    rets_20d = []
    positive_5d = 0
    available = 0

    for asset in theme_assets:
        q = quotes.get(asset.ticker)
        if not q:
            continue
        available += 1
        if q.get("ret_1d") is not None:
            rets_1d.append(q["ret_1d"])
        if q.get("ret_5d") is not None:
            rets_5d.append(q["ret_5d"])
            if q["ret_5d"] > 0:
                positive_5d += 1
        if q.get("ret_20d") is not None:
            rets_20d.append(q["ret_20d"])

    avg_1d = statistics.mean(rets_1d) if rets_1d else None
    avg_5d = statistics.mean(rets_5d) if rets_5d else None
    avg_20d = statistics.mean(rets_20d) if rets_20d else None
    breadth = positive_5d / len(rets_5d) if rets_5d else 0

    score = 50
    if avg_5d is not None:
        score += max(min(avg_5d * 3.0, 20), -20)
    if avg_20d is not None:
        score += max(min(avg_20d * 1.0, 20), -20)
    score += min(news_count * 3, 15)
    score += breadth * 10
    score = max(0, min(100, score))

    if score >= 75:
        status = "강하게 뜨는 중"
    elif score >= 62:
        status = "관심 증가"
    elif score >= 50:
        status = "관찰"
    else:
        status = "약함"

    if news_count >= 4 and (avg_5d is None or avg_5d < 1.5):
        early_signal = "뉴스 선행 가능성"
    else:
        early_signal = ""

    return {
        "score": round(score, 1),
        "status": status,
        "early_signal": early_signal,
        "avg_1d": avg_1d,
        "avg_5d": avg_5d,
        "avg_20d": avg_20d,
        "breadth": breadth,
        "news_count": news_count,
        "available_assets": available,
    }


def group_assets_by_theme(assets: list[Asset]) -> dict[str, list[Asset]]:
    grouped: dict[str, list[Asset]] = {}
    for asset in assets:
        grouped.setdefault(asset.theme_id, []).append(asset)
    return grouped


def make_theme_rankings(assets: list[Asset], quotes: dict[str, dict[str, Any]], news_by_theme: dict[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    grouped = group_assets_by_theme(assets)
    rankings = []

    for theme_id, theme_assets in grouped.items():
        theme_name = theme_assets[0].theme_name
        news_count = len(news_by_theme.get(theme_id, []))
        score = theme_score(theme_assets, quotes, news_count)
        rankings.append({
            "theme_id": theme_id,
            "theme_name": theme_name,
            "assets": theme_assets,
            **score,
        })

    rankings.sort(key=lambda x: x["score"], reverse=True)
    return rankings


def representative_asset_line(asset: Asset, quote: dict[str, Any] | None) -> str:
    if not quote:
        return f"{asset.ticker}: 가격 조회 실패"

    return (
        f"{asset.ticker} "
        f"{format_price(quote.get('price'), quote.get('currency', ''))}, "
        f"1D {format_pct(quote.get('ret_1d'))}, "
        f"5D {format_pct(quote.get('ret_5d'))}, "
        f"20D {format_pct(quote.get('ret_20d'))}"
    )


def build_human_sections(rankings: list[dict[str, Any]], quotes: dict[str, dict[str, Any]], news_by_theme: dict[str, list[dict[str, str]]]) -> str:
    lines = []

    top = rankings[0] if rankings else None
    rising = [r for r in rankings if r["status"] in ("강하게 뜨는 중", "관심 증가")]
    early = [r for r in rankings if r["early_signal"]]

    lines.append("<b>🤖 시장 테마 레이더</b>")
    lines.append("목적: 개별 종목 추천이 아니라, 시장에서 뜨는 테마를 조기 감지합니다.")

    if top:
        conclusion = f"오늘 가장 강한 테마는 <b>{html.escape(top['theme_name'])}</b>입니다."
        if early:
            conclusion += f" 조기 감지 후보는 <b>{html.escape(early[0]['theme_name'])}</b>입니다."
        lines.append(f"\n<b>1. 오늘의 결론</b>\n{conclusion}")

    lines.append("\n<b>2. 테마 랭킹</b>")
    for idx, r in enumerate(rankings, start=1):
        extra = f" / {r['early_signal']}" if r["early_signal"] else ""
        lines.append(
            f"{idx}) <b>{html.escape(r['theme_name'])}</b> "
            f"점수 {r['score']}/100 | {html.escape(r['status'])}{html.escape(extra)} | "
            f"1D {format_pct(r['avg_1d'])}, 5D {format_pct(r['avg_5d'])}, 20D {format_pct(r['avg_20d'])}, "
            f"뉴스 {r['news_count']}건"
        )

    lines.append("\n<b>3. 테마별 대표 ETF/종목</b>")
    for r in rankings:
        asset_lines = []
        for asset in r["assets"][:3]:
            asset_lines.append(representative_asset_line(asset, quotes.get(asset.ticker)))
        lines.append(f"• <b>{html.escape(r['theme_name'])}</b>: " + " / ".join(html.escape(x) for x in asset_lines))

    lines.append("\n<b>4. 코드 설명</b>")
    explain_assets = []
    seen = set()
    for r in rankings:
        for asset in r["assets"]:
            if asset.ticker in seen:
                continue
            seen.add(asset.ticker)
            explain_assets.append(asset)
            if len(explain_assets) >= 7:
                break
        if len(explain_assets) >= 7:
            break

    for asset in explain_assets:
        lines.append(f"• <b>{html.escape(asset.ticker)}</b>: {html.escape(asset.plain_explain)}")

    lines.append("\n<b>5. 숫자 의미</b>")
    lines.append("• 1D: 전 거래일 대비 등락률")
    lines.append("• 5D: 최근 약 5거래일 등락률")
    lines.append("• 20D: 최근 약 20거래일 등락률")
    lines.append("• 점수: 가격 흐름 + 상승 종목 비율 + 뉴스량을 합친 내부 관찰 점수")
    lines.append("• 뉴스 선행 가능성: 가격은 아직 약하지만 관련 뉴스가 늘어나는 상태")

    return "\n".join(lines)


def news_text_for_ai(news_by_theme: dict[str, list[dict[str, str]]], limit_per_theme: int = 3) -> str:
    lines = []
    for theme_id, items in news_by_theme.items():
        for item in items[:limit_per_theme]:
            lines.append(f"[{theme_id}] {item.get('title','')}")
    return "\n".join(lines)


def ranking_text_for_ai(rankings: list[dict[str, Any]]) -> str:
    lines = []
    for r in rankings:
        lines.append(
            f"{r['theme_name']}: score={r['score']}, status={r['status']}, "
            f"early={r['early_signal']}, 1D={format_pct(r['avg_1d'])}, "
            f"5D={format_pct(r['avg_5d'])}, 20D={format_pct(r['avg_20d'])}, "
            f"news={r['news_count']}"
        )
    return "\n".join(lines)


def call_openai_summary(rankings: list[dict[str, Any]], news_by_theme: dict[str, list[dict[str, str]]]) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY가 없어 AI 요약은 생략했습니다."

    prompt = f"""
너는 한국어 투자 테마 모니터링 보조자다.
아래 테마 랭킹과 뉴스 제목을 보고, '전반적인 시장 흐름에서 어떤 테마가 뜨는지'를 설명해라.

중요:
- 개별 종목 매수 추천 금지
- 종목 가격 나열보다 테마 중심으로 설명
- 왜 그 테마가 뜨는지, 아직 조기인지 이미 오른 건지 구분
- 확실하지 않으면 확실하지 않다고 말하기
- 6줄 이내
- 한국어

[테마 랭킹]
{ranking_text_for_ai(rankings)}

[뉴스 제목]
{news_text_for_ai(news_by_theme)}
""".strip()

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You write concise Korean market theme radar summaries."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=40)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"AI 요약 실패: {e}"


def build_news_links(rankings: list[dict[str, Any]], news_by_theme: dict[str, list[dict[str, str]]], max_total: int = 8) -> str:
    lines = []
    count = 0
    for r in rankings:
        items = news_by_theme.get(r["theme_id"], [])
        if not items:
            continue
        lines.append(f"• <b>{html.escape(r['theme_name'])}</b>")
        for item in items[:2]:
            count += 1
            title = html.escape(item.get("title", ""))
            link = html.escape(item.get("link", ""))
            if link:
                lines.append(f"  - <a href=\"{link}\">{title}</a>")
            else:
                lines.append(f"  - {title}")
            if count >= max_total:
                return "\n".join(lines)
    return "\n".join(lines) if lines else "관련 뉴스 없음"


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > limit and current:
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
            print(f"가격 조회 실패: {asset.ticker} / {e}")

    news_by_theme = collect_news_by_theme(assets)
    rankings = make_theme_rankings(assets, quotes, news_by_theme)
    ai_summary = call_openai_summary(rankings, news_by_theme)

    now_kst = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST")

    message = "\n\n".join([
        f"<b>실행시각</b>: {html.escape(now_kst)}",
        build_human_sections(rankings, quotes, news_by_theme),
        f"<b>🧠 AI 테마 해석</b>\n{html.escape(ai_summary)}",
        f"<b>📰 참고 뉴스</b>\n{build_news_links(rankings, news_by_theme)}",
        "※ 투자 판단 보조용입니다. 자동매수 없음.",
    ])

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
