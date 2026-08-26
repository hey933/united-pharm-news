"""
한국유나이티드제약 뉴스 클리핑 - 구글 뉴스 RSS 수집기

약업신문(yakup.com), 데일리팜(dailypharm.com), 팜뉴스(pharmnews.com),
다음뉴스(v.daum.net)에서 구글 뉴스 RSS의 site: 필터를 이용해
'한국유나이티드제약' 관련 기사를 모아 articles.json에 누적 저장한다.

두 가지 모드:
  - daily (기본값):  최근 기사 위주로 증분 수집. GitHub Actions가 매일 09:00 KST에 실행.
  - backfill:        최근 YEARS_BACK년치를 연도별로 쪼개어 검색 (after:/before: 날짜
                     필터). 구글 뉴스 RSS는 검색 1건당 최신순 상위 결과 위주로만
                     돌려주기 때문에, 연도별로 나눠 질의해야 과거 기사를 더 많이
                     모을 수 있다. 처음 한 번(또는 가끔) 수동으로 돌리는 용도.

사용 예:
  python fetch_news.py                # 평소 daily 모드
  python fetch_news.py --mode backfill   # 10년치 백필
"""

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

KEYWORD = "한국유나이티드제약"

# source_id: (표시 이름, 구글 뉴스 site: 필터에 쓸 도메인)
SOURCES = {
    "yakup": ("약업신문", "yakup.com"),
    "dailypharm": ("데일리팜", "dailypharm.com"),
    "pharmnews": ("팜뉴스", "pharmnews.com"),
    "daum": ("다음뉴스", "v.daum.net"),
}

DATA_FILE = "articles.json"
MAX_ITEMS_PER_SOURCE = 60   # daily 모드에서 파일이 무한정 커지지 않도록 소스별 보관 개수 제한
REQUEST_DELAY_SEC = 1.5     # 구글에 너무 빨리 연속 요청하지 않기 위한 딜레이
YEARS_BACK = 10             # backfill 모드에서 몇 년 전까지 거슬러 올라갈지


def build_rss_url(keyword: str, domain: str, start: str = None, end: str = None) -> str:
    query = f'"{keyword}" site:{domain}'
    if start and end:
        query += f" after:{start} before:{end}"
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


def fetch_rss(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def parse_items(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        desc_raw = (item.findtext("description") or "").strip()
        # 구글 뉴스 description은 HTML 스니펫이라 태그만 제거
        desc = re.sub(r"<[^>]+>", "", desc_raw).strip()
        items.append({
            "title": title,
            "url": link,
            "pub_date": pub_date,
            "summary": desc,
        })
    return items


def load_existing():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"updated_at": None, "articles": []}


def dedup_key(title: str, pub_date: str) -> str:
    """제목+발행일시가 완전히 같으면 같은 기사로 간주한다.
    (구글 뉴스가 같은 기사를 다른 추적 링크로 두 번 주는 경우가 있어서
    URL이 아니라 이 값으로 중복을 판단한다.)"""
    norm_title = re.sub(r"\s+", " ", title).strip()
    return f"{norm_title}||{pub_date.strip()}"


def merge_items(existing: dict, source_id: str, label: str, items: list, limit: int = None):
    count_new = 0
    for it in (items[:limit] if limit else items):
        key = dedup_key(it["title"], it["pub_date"])
        if key in existing:
            continue
        existing[key] = {
            "source": source_id,
            "label": label,
            "title": it["title"],
            "summary": it["summary"],
            "url": it["url"],
            "pub_date": it["pub_date"],
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        count_new += 1
    return count_new


def run_daily(existing: dict):
    for source_id, (label, domain) in SOURCES.items():
        url = build_rss_url(KEYWORD, domain)
        try:
            items = parse_items(fetch_rss(url))
        except Exception as e:
            print(f"[경고] {label} 수집 실패: {e}")
            continue
        n = merge_items(existing, source_id, label, items, limit=MAX_ITEMS_PER_SOURCE)
        print(f"{label}: 신규 {n}건 (후보 {len(items)}건)")
        time.sleep(REQUEST_DELAY_SEC)


def run_backfill(existing: dict):
    current_year = datetime.now().year
    years = range(current_year - YEARS_BACK + 1, current_year + 1)
    for source_id, (label, domain) in SOURCES.items():
        total_new = 0
        for year in years:
            start = f"{year}-01-01"
            end = f"{year + 1}-01-01"
            url = build_rss_url(KEYWORD, domain, start, end)
            try:
                items = parse_items(fetch_rss(url))
            except Exception as e:
                print(f"[경고] {label} {year}년 수집 실패: {e}")
                continue
            n = merge_items(existing, source_id, label, items)
            total_new += n
            print(f"{label} {year}년: 신규 {n}건 (후보 {len(items)}건)")
            time.sleep(REQUEST_DELAY_SEC)
        print(f">> {label} 총 신규 {total_new}건")


def sort_key(a):
    try:
        return datetime.strptime(a["pub_date"], "%a, %d %b %Y %H:%M:%S %Z")
    except Exception:
        return datetime.min


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "backfill"], default="daily")
    args = parser.parse_args()

    data = load_existing()

    # 기존 articles.json을 새 중복 판정 기준(제목+발행일시)으로 다시 키를 매겨
    # 불러온다. 이전 방식(URL 기준)으로 저장돼 이미 중복이 들어있던 기사도
    # 여기서 한 번에 정리된다. 같은 키가 여럿이면 먼저 나온 것(대개 더 먼저
    # 수집된 것)을 유지한다.
    existing = {}
    removed_dup = 0
    for a in data.get("articles", []):
        key = dedup_key(a.get("title", ""), a.get("pub_date", ""))
        if key in existing:
            removed_dup += 1
            continue
        existing[key] = a
    if removed_dup:
        print(f"기존 데이터에서 중복 {removed_dup}건 정리")

    if args.mode == "backfill":
        run_backfill(existing)
    else:
        run_daily(existing)

    all_articles = sorted(existing.values(), key=sort_key, reverse=True)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "articles": all_articles,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료: 총 {len(all_articles)}건 저장 -> {DATA_FILE}")


if __name__ == "__main__":
    main()

