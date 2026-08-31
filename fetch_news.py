"""
한국유나이티드제약 뉴스 클리핑 - 구글 뉴스 RSS 수집기

약업신문(yakup.com), 데일리팜(dailypharm.com), 팜뉴스(pharmnews.com),
다음뉴스(v.daum.net)에서 구글 뉴스 RSS의 site: 필터를 이용해
'한국유나이티드제약' 관련 기사를 모아 articles.json에 누적 저장한다.

두 가지 모드:
  - daily (기본값):  최근 기사 위주로 증분 수집. GitHub Actions가 매일 09:00 KST에 실행.
  - backfill:        최근 YEARS_BACK년치를 연도별로 쪼개어 검색 (after:/before: 날짜
                     필터). 처음 한 번(또는 가끔) 수동으로 돌리는 용도.

날짜 보정 (2단계):
  1) 최근(RECENT_VERIFY_WINDOW_DAYS일 이내)으로 찍힌 항목만, 실제 기사
     페이지에 들어가 진짜 게재일을 확인한다. 구글 뉴스 링크는 실제 기사가
     아니라 news.google.com의 중간 링크인 경우가 많아, 그 안에 인코딩된
     실제 URL을 먼저 디코딩해서 찾아낸 뒤 그 페이지에서 날짜를 읽는다.
  2) (backfill 전용) 그래도 남는 이상치를 위해, 검색한 연도와 pubDate의
     연도가 크게 어긋나면 검색 연도로 강제 보정하고 date_estimated=True로
     표시한다.

사용 예:
  python fetch_news.py                  # 평소 daily 모드
  python fetch_news.py --mode backfill  # 10년치 백필
"""

import argparse
import base64
import html
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
MAX_ITEMS_PER_SOURCE = 60        # daily 모드에서 소스별 보관 개수 제한
REQUEST_DELAY_SEC = 1.5          # 구글 RSS 요청 사이 딜레이
PAGE_FETCH_DELAY_SEC = 0.6       # 원문 페이지 확인 요청 사이 딜레이
YEARS_BACK = 10                  # backfill 모드에서 몇 년 전까지
RECENT_VERIFY_WINDOW_DAYS = 30   # 이 안에 있는 날짜만 원문에서 재검증

RSS_DATE_FMT = "%a, %d %b %Y %H:%M:%S %Z"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def build_rss_url(keyword: str, domain: str, start: str = None, end: str = None) -> str:
    query = f'"{keyword}" site:{domain}'
    if start and end:
        query += f" after:{start} before:{end}"
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def parse_rss_dt(pub_date: str):
    try:
        return datetime.strptime(pub_date, RSS_DATE_FMT)
    except Exception:
        return None


def pub_year(pub_date: str):
    dt = parse_rss_dt(pub_date)
    return dt.year if dt else None


def sort_key(a):
    dt = parse_rss_dt(a.get("pub_date", ""))
    return dt if dt else datetime.min


def to_rss_str(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def parse_any_date(s: str):
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


DATE_META_PATTERNS = [
    r'property=["\']article:published_time["\']\s+content=["\']([^"\']+)["\']',
    r'itemprop=["\']datePublished["\']\s+content=["\']([^"\']+)["\']',
    r'name=["\']date["\']\s+content=["\']([^"\']+)["\']',
    r'name=["\']pubdate["\']\s+content=["\']([^"\']+)["\']',
]


def extract_date_from_html(html_text: str):
    for pattern in DATE_META_PATTERNS:
        m = re.search(pattern, html_text, re.IGNORECASE)
        if m:
            d = parse_any_date(m.group(1))
            if d:
                return d
    # 국내 언론사 페이지에 흔한 '입력 2016.05.20' / '승인 2016.05.20' 형태
    m = re.search(r'(?:입력|승인|등록)\D{0,6}(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})', html_text)
    if not m:
        m = re.search(r'(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})', html_text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    return None


# 실제 언론사 도메인 목록 (구글 링크/토큰 안에서 이 도메인이 포함된 URL을
# 찾아 실제 기사로 한 번 더 들어가기 위함)
TARGET_DOMAINS = ["yakup.com", "dailypharm.com", "pharmnews.com", "v.daum.net", "daum.net"]
_DOMAIN_PATTERN = re.compile(
    r'(?:https?:)?//[^\s"\'<>\\]*(?:' + "|".join(re.escape(d) for d in TARGET_DOMAINS) + r')[^\s"\'<>\\]*'
)


def _normalize_candidate_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    return url


def decode_google_token(google_url: str):
    """구글 뉴스 링크(news.google.com/rss/articles/<토큰>)의 <토큰>이 (구버전
    방식으로) 원본 URL을 그대로 담고 있는 경우, 페이지 요청 없이 바로
    디코딩해서 뽑아낸다. 요즘은 대부분 아래 batchexecute 방식이 필요한
    새 형식이라 여기서 실패하는 게 정상이며, 그 경우 None을 돌려준다."""
    m = re.search(r'/articles/([^?/]+)', google_url)
    if not m:
        return None
    token = m.group(1)
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(padded)
        except Exception:
            continue
        text = raw.decode("utf-8", errors="ignore")
        dm = _DOMAIN_PATTERN.search(text)
        if dm:
            return _normalize_candidate_url(dm.group(0))
    return None


_BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def decode_google_token_via_api(google_url: str, timeout: int = 10):
    """구글이 요즘 쓰는 새 형식(AU_yqL...) 토큰은 로컬 디코딩이 안 되고,
    구글 뉴스 기사 페이지에 심어진 서명(signature)·타임스탬프를 이용해
    구글 내부 batchexecute API를 호출해야 실제 URL을 돌려받을 수 있다.
    (커뮤니티에 알려진 방식 - 구글이 페이지 구조를 바꾸면 깨질 수 있음)"""
    try:
        req = urllib.request.Request(google_url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return None, f"api_page_fetch_error:{type(e).__name__}"

    id_m = re.search(r'data-n-a-id="([^"]+)"', page)
    sg_m = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts_m = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (id_m and sg_m and ts_m):
        return None, "api_signature_not_found"

    article_id, signature, timestamp = id_m.group(1), sg_m.group(1), ts_m.group(1)

    inner = json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        article_id, timestamp, signature,
    ])
    f_req = json.dumps([[["Fbv4je", inner, None, "generic"]]])
    body = "f.req=" + urllib.parse.quote(f_req)

    try:
        req = urllib.request.Request(
            _BATCHEXECUTE_URL,
            data=body.encode("utf-8"),
            headers={**HTTP_HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_text = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return None, f"api_call_error:{type(e).__name__}"

    m = re.search(r'\\?"(https?://[^"\\]+)\\?"', resp_text)
    if not m:
        unescaped = resp_text.replace('\\"', '"').replace("\\\\", "\\").replace("\\/", "/")
        m = re.search(r'"(https?://[^"]+)"', unescaped)
    if not m:
        return None, "api_response_no_url"
    return m.group(1), "ok"


def _normalize_page_text(raw: str) -> str:
    text = raw.replace("\\/", "/")
    text = html.unescape(text)
    try:
        text = urllib.parse.unquote(text)
    except Exception:
        pass
    return text


def resolve_source_page(google_url: str, timeout: int = 10):
    """구글 뉴스 RSS의 link는 실제 기사가 아니라 구글의 중간 링크인
    경우가 많다. 다음 순서로 실제 기사 URL을 찾는다:
      1) 토큰 자체를 base64 디코딩 (구버전 형식이면 요청 없이 바로 성공)
      2) 안 되면 구글의 batchexecute API로 디코딩 (신버전 형식, 요청 2번)
      3) 그래도 안 되면 구글 링크를 직접 열어 리디렉션/본문 속 URL을 찾는다
    (실제 기사 URL, 그 페이지 HTML, 사유) 튜플을 돌려준다."""
    token_url = decode_google_token(google_url)
    via_api = False
    reason_prefix = ""
    if not token_url:
        api_url, api_reason = decode_google_token_via_api(google_url, timeout=timeout)
        if api_url:
            token_url = api_url
            via_api = True
        else:
            reason_prefix = api_reason + "|"

    if token_url:
        try:
            req = urllib.request.Request(token_url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ok_reason = "ok_api_decode" if via_api else "ok_local_decode"
                return token_url, resp.read().decode("utf-8", errors="ignore"), ok_reason
        except Exception as e:
            return token_url, None, f"article_fetch_error:{type(e).__name__}"

    try:
        req = urllib.request.Request(google_url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return None, None, f"{reason_prefix}google_fetch_error:{type(e).__name__}"

    if any(d in final_url for d in TARGET_DOMAINS):
        return final_url, raw, "ok_direct_redirect"

    text = _normalize_page_text(raw)
    m = _DOMAIN_PATTERN.search(text)
    if not m:
        return None, None, f"{reason_prefix}no_real_url_found_in_page"
    real_url = _normalize_candidate_url(m.group(0))

    try:
        req2 = urllib.request.Request(real_url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req2, timeout=timeout) as resp2:
            return real_url, resp2.read().decode("utf-8", errors="ignore"), "ok_via_embedded_url"
    except Exception as e:
        return real_url, None, f"article_fetch_error:{type(e).__name__}"


def verify_recent_date(it: dict):
    """구글이 준 pubDate가 최근으로 찍혀 있으면, 실제 기사 페이지에
    들어가 진짜 게재일과 비교해 어긋나면 바로잡는다. 실패해도 구글 값은
    그대로 두되, 진단을 위해 사유 문자열을 돌려준다."""
    g_dt = parse_rss_dt(it["pub_date"])
    if g_dt is None:
        return "skip_no_pubdate"
    if (datetime.utcnow() - g_dt).days > RECENT_VERIFY_WINDOW_DAYS:
        return "skip_not_recent"

    real_url, html_text, reason = resolve_source_page(it["url"])
    if real_url:
        it["url"] = real_url  # 클릭했을 때 구글 대신 실제 기사로 바로 가도록 교체
    if html_text is None:
        return reason

    real_dt = extract_date_from_html(html_text)
    if real_dt is None:
        return "date_pattern_not_found"

    if abs((real_dt - g_dt).days) >= 3:
        it["pub_date"] = to_rss_str(real_dt)
        it["date_estimated"] = False
        return "fixed"
    return "confirmed_same"


def dedup_key(source_id: str, title: str) -> str:
    """같은 소스에서 제목이 완전히 같으면 같은 기사로 본다."""
    norm_title = re.sub(r"\s+", " ", title).strip()
    return f"{source_id}||{norm_title}"


def load_existing():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"updated_at": None, "articles": []}


def build_existing_index(data: dict):
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
                old["url"] = record["url"]
                if record["summary"]:
                    old["summary"] = record["summary"]
                count_fixed += 1
            continue
        existing[key] = record
        count_new += 1
    return count_new, count_fixed


def verify_items(items: list):
    """반환값: (고쳐진 건수, 사유별 집계 dict) - 로그로 어디서 막히는지
    바로 보이게 하기 위함."""
    fixed = 0
    reasons = {}
    for it in items:
        try:
            reason = verify_recent_date(it)
        except Exception as e:
            reason = f"unexpected_error:{type(e).__name__}"
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
            if reason == "fixed":
                fixed += 1
        time.sleep(PAGE_FETCH_DELAY_SEC)
    return fixed, reasons


def format_reasons(reasons: dict) -> str:
    if not reasons:
        return ""
    return " [" + ", ".join(f"{k}:{v}" for k, v in reasons.items()) + "]"


def run_daily(existing: dict):
    for source_id, (label, domain) in SOURCES.items():
        url = build_rss_url(KEYWORD, domain)
        try:
            items = parse_items(http_get(url))
        except Exception as e:
            print(f"[경고] {label} 수집 실패: {e}")
            continue

        verified, reasons = verify_items(items)
        n, fixed = merge_items(existing, source_id, label, items, limit=MAX_ITEMS_PER_SOURCE)
        note = f" / 원문 대조로 날짜 보정 {verified}건 / 갱신 {fixed}건" if (verified or fixed) else ""
        print(f"{label}: 신규 {n}건 (후보 {len(items)}건){note}{format_reasons(reasons)}")
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
                items = parse_items(http_get(url))
            except Exception as e:
                print(f"[경고] {label} {year}년 수집 실패: {e}")
                continue

            # 1단계: 최근으로 찍힌 항목만 원문 대조 (당해 연도 검색에서만 해당)
            verified, reasons = verify_items(items)

            # 2단계: 그래도 검색 연도와 크게 어긋나면 검색 연도로 강제 보정
            year_fixed = 0
            for it in items:
                y = pub_year(it["pub_date"])
                if y is None or y < year or y > year + 1:
                    it["pub_date"] = f"Mon, 01 Jan {year} 00:00:00 GMT"
                    it["date_estimated"] = True
                    year_fixed += 1

            n, fixed = merge_items(existing, source_id, label, items)
            total_new += n
            total_fixed += fixed
            parts = []
            if verified:
                parts.append(f"원문 대조 보정 {verified}건")
            if year_fixed:
                parts.append(f"연도 강제 보정 {year_fixed}건")
            if fixed:
                parts.append(f"기존 항목 갱신 {fixed}건")
            note = f" ({', '.join(parts)})" if parts else ""
            print(f"{label} {year}년: 신규 {n}건 (후보 {len(items)}건){note}{format_reasons(reasons)}")
            time.sleep(REQUEST_DELAY_SEC)
        print(f">> {label} 총 신규 {total_new}건 / 갱신 {total_fixed}건")


MANUAL_DATES_FILE = "manual_dates.json"


def load_manual_corrections():
    """manual_dates.json이 있으면 {"제목에 포함된 문자열": "YYYY-MM-DD"} 형태로
    읽어서, 자동 수집으로 못 고친 기사의 날짜를 강제로 맞춰주는 최후의
    안전장치로 쓴다. 파일이 없으면 그냥 빈 채로 넘어간다."""
    try:
        with open(MANUAL_DATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[경고] {MANUAL_DATES_FILE} 읽기 실패: {e}")
        return {}


def apply_manual_corrections(existing: dict, corrections: dict) -> int:
    if not corrections:
        return 0
    applied = 0
    for record in existing.values():
        title = record.get("title", "")
        for needle, date_str in corrections.items():
            if needle.startswith("_"):
                continue
            if needle in title:
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    print(f"[경고] manual_dates.json 날짜 형식 오류: {date_str} (YYYY-MM-DD여야 함)")
                    continue
                record["pub_date"] = to_rss_str(dt)
                record["date_estimated"] = False
                applied += 1
                break
    return applied


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

    corrections = load_manual_corrections()
    applied = apply_manual_corrections(existing, corrections)
    if applied:
        print(f"manual_dates.json으로 수동 보정 {applied}건 적용")

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
