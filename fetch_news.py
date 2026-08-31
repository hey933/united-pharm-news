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

날짜 보정:
  구글 뉴스 RSS의 pubDate는 가끔 '실제 발행일'이 아니라 '구글이 그 링크를
  최근에 다시 색인한 날짜'로 나온다. 특히 오래된 기사를 연도별 검색으로
  찾을 때 이 문제가 두드러진다 (예: 2010년 검색인데 pubDate는 오늘 날짜).
  이를 막기 위해, 검색한 연도와 실제 pubDate의 연도가 크게 어긋나면 구글
  값을 버리고 검색 연도로 보정한 뒤 date_estimated=True로 표시한다.

사용 예:
  python fetch_news.py                  # 평소 daily 모드
  python fetch_news.py --mode backfill  # 10년치 백필 (재실행하면 잘못된
                                          # 날짜도 다시 검사해 고쳐진다)
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

RSS_DATE_FMT = "%a, %d %b %Y %H:%M:%S %Z"


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
        desc = re.sub(r"<[^>]+>", "", desc_raw).strip()
        items.append({
            "title": title,
            "url": link,
            "pub_date": pub_date,
            "summary": desc,
            "date_estimated": False,
        })
    return items


def pub_year(pub_date: str):
    try:
        return datetime.strptime(pub_date, RSS_DATE_FMT).year
    except Exception:
        return None


def sort_key(a):
    try:
        return datetime.strptime(a["pub_date"], RSS_DATE_FMT)
    except Exception:
        return datetime.min


def dedup_key(source_id: str, title: str) -> str:
    """같은 소스에서 제목이 완전히 같으면 같은 기사로 본다.
    (예전엔 제목+발행일시로 판정했는데, 날짜 보정 때문에 같은 기사의
    pubDate 값이 실행마다 달라질 수 있어 제목 기준으로 바꿨다.)"""
    norm_title = re.sub(r"\s+", " ", title).strip()
    return f"{source_id}||{norm_title}"


def load_existing():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"updated_at": None, "articles": []}


def build_existing_index(data: dict):
    """articles.json을 dedup_key 기준으로 다시 인덱싱한다. 같은 키가
    여럿이면(예전 방식으로 저장돼 중복이던 것) 날짜가 추정이 아닌 쪽을
    우선 남긴다."""
    existing = {}
    removed_dup = 0
    for a in data.get("articles", []):
        key = dedup_key(a.get("source", ""), a.get("title", ""))
        if key in existing:
            removed_dup += 1
            if existing[key].get("date_estimated") and not a.get("date_estimated"):
                existing[key] = a
            continue
        existing[key] = a
    return existing, removed_dup


def merge_items(existing: dict, source_id: str, label: str, items: list, limit: int = None):
    count_new = 0
    count_fixed = 0
    for it in (items[:limit] if limit else items):
        key = dedup_key(source_id, it["title"])
        record = {
            "source": source_id,
            "label": label,
            "title": it["title"],
            "summary": it["summary"],
            "url": it["url"],
            "pub_date": it["pub_date"],
            "date_estimated": it.get("date_estimated", False),
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        if key in existing:
            old = existing[key]
            if old.get("pub_date") != record["pub_date"] or old.get("date_estimated") != record["date_estimated"]:
                old["pub_date"] = record["pub_date"]
                old["date_estimated"] = record["date_estimated"]
                if record["summary"]:
                    old["summary"] = record["summary"]
                count_fixed += 1
            continue
        existing[key] = record
        count_new += 1
    return count_new, count_fixed


def run_daily(existing: dict):
    for source_id, (label, domain) in SOURCES.items():
        url = build_rss_url(KEYWORD, domain)
        try:
            items = parse_items(fetch_rss(url))
        except Exception as e:
            print(f"[경고] {label} 수집 실패: {e}")
            continue
        n, fixed = merge_items(existing, source_id, label, items, limit=MAX_ITEMS_PER_SOURCE)
        note = f" / 갱신 {fixed}건" if fixed else ""
        print(f"{label}: 신규 {n}건 (후보 {len(items)}건){note}")
        time.sleep(REQUEST_DELAY_SEC)


def run_backfill(existing: dict):
    current_year = datetime.now().year
    years = range(current_year - YEARS_BACK + 1, current_year + 1)
    for source_id, (label, domain) in SOURCES.items():
        total_new = 0
        total_fixed = 0
        for year in years:
            start = f"{year}-01-01"
            end = f"{year + 1}-01-01"
            url = build_rss_url(KEYWORD, domain, start, end)
            try:
                items = parse_items(fetch_rss(url))
            except Exception as e:
                print(f"[경고] {label} {year}년 수집 실패: {e}")
                continue

            date_fixed_count = 0
            for it in items:
                y = pub_year(it["pub_date"])
                # 이번 검색은 {year}년으로 범위를 좁힌 결과다. 돌아온 pubDate가
                # 그 연도(혹은 다음 연도 1월 초까지)를 크게 벗어나면 구글이
                # 재색인 날짜를 준 것으로 보고, 검색 연도로 보정한다.
                if y is None or y < year or y > year + 1:
                    it["pub_date"] = f"Mon, 01 Jan {year} 00:00:00 GMT"
                    it["date_estimated"] = True
                    date_fixed_count += 1

            n, fixed = merge_items(existing, source_id, label, items)
            total_new += n
            total_fixed += fixed
            note = f" (날짜 보정 {date_fixed_count}건 / 기존 항목 갱신 {fixed}건)" if (date_fixed_count or fixed) else ""
            print(f"{label} {year}년: 신규 {n}건 (후보 {len(items)}건){note}")
            time.sleep(REQUEST_DELAY_SEC)
        print(f">> {label} 총 신규 {total_new}건 / 갱신 {total_fixed}건")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "backfill"], default="daily")
    args = parser.parse_args()

    data = load_existing()
    existing, removed_dup = build_existing_index(data)
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
