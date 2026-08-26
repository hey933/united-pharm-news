# 한국유나이티드제약 뉴스 클리핑 (자동 업데이트)

매일 한국시간(KST) 오전 9시에 약업신문 · 데일리팜 · 팜뉴스 · 다음뉴스에서
"한국유나이티드제약" 관련 기사를 구글 뉴스 RSS로 수집해 `articles.json`에
쌓고, `index.html`이 그걸 읽어서 보여주는 정적 홈페이지입니다.

## 배포 방법 (GitHub Pages, 무료)

1. GitHub에 새 저장소를 만듭니다 (예: `united-pharm-news`).
2. 이 폴더의 파일 전체(`index.html`, `articles.json`, `fetch_news.py`,
   `.github/workflows/update-news.yml`)를 그 저장소에 업로드합니다.
3. 저장소 **Settings → Pages**로 이동해 Source를 `main` 브랜치(또는
   `Deploy from a branch`)로 설정합니다. 잠시 후 `https://<계정>.github.io/<저장소명>/`
   주소가 생성됩니다. 이게 실제 홈페이지 주소입니다.
4. 저장소 **Settings → Actions → General**에서 "Workflow permissions"을
   **Read and write permissions**로 바꿔줍니다 (Actions가 articles.json을
   커밋할 수 있어야 하기 때문입니다).
5. 저장소의 **Actions** 탭에서 `유나이티드제약 뉴스 클리핑 업데이트` 워크플로우를
   한 번 수동 실행(`Run workflow`)해서 정상 동작하는지 확인합니다.
6. 이후로는 매일 KST 09:00에 자동으로 실행되어 `articles.json`이 갱신되고,
   홈페이지에도 자동 반영됩니다.

## 참고

- 구글 뉴스 RSS는 무료·무인증이라 API 키가 필요 없습니다.
- 다음뉴스는 포털이라 기사가 `v.daum.net` 도메인으로 재발행되는 경우 위주로
  잡힙니다. 원문사와 재발행 시점 차이로 일부 기사가 안 잡힐 수 있습니다.
- 검색 키워드나 대상 언론사를 바꾸고 싶으면 `fetch_news.py` 상단의
  `KEYWORD`, `SOURCES` 값만 수정하면 됩니다.
- GitHub Actions 무료 사용량 안에서 하루 1회 실행은 부담 없는 수준입니다.
