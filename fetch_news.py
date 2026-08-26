"""
한국유나이티드제약 뉴스 클리핑 - 구글 뉴스 RSS 수집기

약업신문(yakup.com), 데일리팜(dailypharm.com), 팜뉴스(pharmnews.com),
다음뉴스(v.daum.net)에서 구글 뉴스 RSS의 site: 필터를 이용해
'한국유나이티드제약' 관련 기사를 모아 articles.json에 누적 저장한다.

GitHub Actions가 매일 KST 09:00에 이 스크립트를 실행한다.
"""

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
MAX_ITEMS_PER_SOURCE = 60   # 파일이 무한정 커지지 않도록 소스별 보관 개수 제한
REQUEST_DELAY_SEC = 1.5     # 구글에 너무 빨리 연속 요청하지 않기 위한 딜레이


def build_rss_url(keyword: str, domain: str) -> str:
    query = f'"{keyword}" site:{domain}'
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


def main():
    data = load_existing()
    existing = {a["url"]: a for a in data.get("articles", [])}

    for source_id, (label, domain) in SOURCES.items():
        url = build_rss_url(KEYWORD, domain)
        try:
            xml_bytes = fetch_rss(url)
            items = parse_items(xml_bytes)
        except Exception as e:
            print(f"[경고] {label} 수집 실패: {e}")
            continue

        count_new = 0
        for it in items[:MAX_ITEMS_PER_SOURCE]:
            if it["url"] in existing:
                continue
            existing[it["url"]] = {
                "source": source_id,
                "label": label,
                "title": it["title"],
                "summary": it["summary"],
                "url": it["url"],
                "pub_date": it["pub_date"],
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }
            count_new += 1

        print(f"{label}: 신규 {count_new}건 (총 후보 {len(items)}건)")
        time.sleep(REQUEST_DELAY_SEC)

    # 최신순 정렬 (pub_date 파싱 실패 항목은 뒤로)
    def sort_key(a):
        try:
            return datetime.strptime(a["pub_date"], "%a, %d %b %Y %H:%M:%S %Z")
        except Exception:
            return datetime.min

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
