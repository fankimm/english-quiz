# 영어 문장 퀴즈

한국어 뜻을 보고 영어 문장을 떠올려 입력하면 채점해 주는 학습용 퀴즈. Day(5문장)별로 묶여 있고, 정답 보기 탭도 있다.

## 두 가지 버전

| | 채점 방식 | 실행 |
|---|---|---|
| **`index.html`** (정적) | 브라우저 JS — 정규화 + 유사도로 ⭕/🔺/❌ + 단어 diff. 한국어 코멘트는 없음. | GitHub Pages 등 정적 호스팅에서 그대로 동작. 더블클릭으로 열어도 됨. |
| **`quiz_server.py`** (로컬, 선택) | `claude` CLI(Haiku) 헤드리스 채점 — 한국어 코멘트 포함, 더 똑똑함. | `python3 quiz_server.py` → http://127.0.0.1:4321 |

GitHub Pages로 배포되는 건 **정적 버전(`index.html`)**이다. 채점은 브라우저에서:

- 대소문자, 구두점, 따옴표, 하이픈, 여분 공백 무시
- `it's`=`its`, `im`=`I'm`, `dont`=`don't` 등 어퍼스트로피 차이 무시
- `gonna`=`going to`, `kinda`=`kind of` 등 구어 축약형 인정
- 완전 일치 ⭕ / 유사도 0.7↑ 거의 맞음 🔺 / 그 외 ❌

입력과 채점 결과는 브라우저 localStorage에 저장돼 새로고침해도 유지된다.

## 문장 추가

`index.html` 안의 `SENTENCES` 배열에 `{en, ko}` 객체를 추가하면 5개 단위로 다음 Day가 자동 생성된다. (로컬 Claude 버전을 같이 쓴다면 `quiz_server.py`의 `DATA`도 같이 갱신.)
