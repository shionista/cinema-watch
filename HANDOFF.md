# 인수인계 (다른 PC에서 이어서 작업하기)

작성: 2026-08-28 · 대상: `cinema_watch.py` v0.3

---

## 1. 새 PC 세팅

```bash
gh repo clone shionista/cinema-watch
cd cinema-watch
python cinema_watch.py
```

- Python 3.10+ 만 있으면 됩니다. **설치할 패키지 없음** (표준 라이브러리 전용).
- Windows Terminal / PowerShell 7 권장 (ANSI·박스문자). 깨지면 자동으로 단순 출력으로 떨어집니다.
- `gh` 미설치 시: `winget install --id GitHub.cli` → `gh auth login` (브라우저 인증).
- 로컬 데이터 파일(`profiles.json`, `favorites.json`, `history.jsonl`, `openlog.jsonl`)은
  gitignore 대상이라 새 PC에는 없습니다. **`openlog.jsonl` 은 예측 정확도의 근거이므로
  기존 PC에서 쓰던 게 있으면 수동으로 복사**해 오세요.

---

## 2. 지금까지 한 일

### 완료된 기능

| | 기능 | 비고 |
|---|---|---|
| 기본 | 좌석 감시 / 예매오픈 감시 | 지역→지점→영화→요일→상영관→좌석 마법사 |
| UI | 방향키 TUI, 브랜드 배너, 라이브 대시보드 | `Chooser`, `print_banner`, `dashboard_lines` |
| UI | 즐겨찾기(브랜드별), `m` 키로 메뉴 복귀 | `Favorites`, `BackToMenu` |
| A | 적응형 폴링 | `Scheduler` |
| B | 잔여석 변동 이력 | `ShowTracker` → `history.jsonl` |
| C | 명당 자동 추천 | `sweet_scores` / `sweet_seats` |
| — | 예매 오픈 패턴 측정·예측 | `OpenRadar`, `Horizon` → `openlog.jsonl` |
| N | 멀티 브랜드 | `Provider` / `LotteProvider` / `MegaboxProvider` / `CgvProvider` |

### 코드 지도 (`cinema_watch.py` 단일 파일, 약 3,100줄)

```
C, UI, Chooser, box_*        터미널·색상·방향키 위젯
Api                          롯데 LCWS HTTP 클라이언트 (paramList multipart)
Provider                     브랜드 추상 계층 (supports_seats / supports_shows)
 ├ LotteProvider             지점·상영표·좌석표 전부
 ├ MegaboxProvider           지점·상영표(잔여석)  ※ 좌석표 불가
 └ CgvProvider               지점만              ※ 상영표 불가
WebJson                      메가박스/CGV 용 JSON HTTP (쿠키·스로틀)
Cinema / Show / SeatMap      브랜드 공통 모델
Catalog                      provider 래핑 + 상영표 캐시 + merge_rows
ShowTracker                  잔여석 변동 관측·기록·통계 (B)
Scheduler                    회차별 확인 간격 결정 (A)
OpenRadar / Horizon          예매 범위 측정·기록·패턴 학습·예측
Watch                        감시 설정 (프로필로 저장)
State / dashboard_lines      라이브 화면 상태와 렌더
worker_seat / worker_open / worker_radar   감시 스레드 3종
wizard_seat / wizard_open / wizard_radar   설정 마법사
pick_brand → run_brand → menu_once → main  진입 흐름
```

### 검증된 API 사실 (재조사 불필요)

**롯데시네마** — `https://www.lottecinema.co.kr/LCWS/...`,
multipart 의 `paramList` 필드에 JSON 한 덩어리. 로그인 불필요.

| 용도 | 경로 / MethodName | 파라미터 |
|---|---|---|
| 극장 목록 | `Cinema/CinemaData.aspx` / `GetCinemaItems` | 없음 |
| 상영표 | `Ticketing/TicketingData.aspx` / `GetPlaySequence` | `cinemaID="1\|1\|1016"` 형식 |
| 좌석표 | `Ticketing/TicketingData.aspx` / `GetSeats` | `cinemaID="1016"` (접두어 없이!) |

**메가박스** — `https://www.megabox.co.kr`, JSON POST. 먼저 `/booking/timetable` GET 으로
세션 쿠키 확보 필요.

| 용도 | 경로 | 파라미터 |
|---|---|---|
| 지역·지점 | `/on/oh/ohb/PlayTime/selectPlayTimeMasterList.do` | `{"playDe":"20260830"}` |
| 상영표+잔여석 | `/on/oh/ohc/Brch/schedulePage.do` | `{"playDe":"20260830","brchNo1":"1372"}` |

> `brchNo1` 이어야 지점 필터가 걸립니다. `brchNo` 로 보내면 전국이 옵니다.

**CGV** — `https://cgv.co.kr/api/v1/...`, JSON GET.

| 용도 | 경로 |
|---|---|
| 지역·극장 | `/api/v1/booking/searchRegnList?coCd=A420` |

### 함정 모음 (같은 실수 반복 방지)

1. **`BookingSeatCount` 는 예매된 수가 아니라 잔여 좌석 수.** 좌석표와 대조해 확인함.
2. **같은 회차가 좌석 구역별로 두 줄로 내려옴** (예: `씨네패밀리` 18석 + `일반` 342석).
   `GetSeats` 는 합친 360석을 주므로 `Catalog.merge_rows` 에서 합산. 안 합치면
   잔여석이 두 값 사이를 오가며 **취소표 오탐**이 남.
3. **리클라이너관은 좌석 번호가 띄엄띄엄** (21관 A열 = 5,6,7,8,12,13…). 통로 때문.
   → 연속석 판정은 `col+1` 인접으로 해야 맞고, 없는 번호 입력 시 검증으로 걸러야 함.
4. **`SweetSpotYN` 은 전 좌석 `N`** (미사용 필드). 명당은 좌표로 계산해야 함.
   `X`=좌우, `Y`=스크린에서 멀어지는 방향(A열이 최소).
5. **클래스 속성 기본값에 `C.RED` 같은 색을 넣으면 안 됨.** import 시점에 값이 고정돼
   `C.disable()` 이 안 먹음. `color_name = "BRED"` 처럼 이름을 두고 런타임에 `getattr`.
6. **파이프 입력 테스트 시** `Chooser` 는 번호 입력 폴백으로 동작. `UI["tty"]` 로 분기.
7. 롯데 극장 목록은 `DivisionCode==1` 만 지역 구분. `2` 는 특별관 묶음이라 중복.

### 성능 실측

- 적응형 폴링: 월드타워·오디세이 3일치 109회차 기준 **첫 주기 113회 → 이후 2~3회 호출**.
- `OpenRadar.scan`: 45일 범위 이진탐색에 6~7회 호출.

---

## 3. 남은 작업 (우선순위 순)

### (1) CGV 상영표 API 확보 — 막힌 지점

- 구 API(`www.cgv.co.kr/common/showtimes/iframeTheater.aspx`, `m.cgv.co.kr/WebApp/...`)는
  **전부 폐기**되어 `https://cgv.co.kr/` 로 301 리다이렉트됨.
- 신규 사이트는 Next.js SPA. 예매 페이지는 `https://cgv.co.kr/cnm/movieBook`.
  브라우저로 네트워크를 관찰했으나 **극장 선택까지만 호출이 잡히고 상영표 호출은 못 봄**.
  확인된 호출: `searchRegnList`, `searchSscnsCdList`, `searchOnlyCgvMovList`,
  `searchGradByRpsntGrad`, `searchAtktTopPostrList`.
- 엔드포인트 이름 추측은 실패. 404 메시지가 내부 경로를 노출함
  (`/api/v1/booking/X` → `No endpoint GET /cnm/atkt/X.`) — 이름만 알면 바로 됨.
- 앱 API `https://api.cgv.co.kr/v1/...` 는 존재하지만 **401** (키/토큰 필요).

**다음 수단: Burp 로 CGV 앱 예매 화면 트래픽 1건 캡처.** iOS/Android 세팅은 이미 있음.
캡처하면 `CgvProvider.showtimes()` 만 채우면 되고 나머지는 그대로 동작
(`supports_shows = True` 로 바꾸면 메뉴가 자동으로 열림).

### (2) J. 오픈 임박 대기 모드

`OpenRadar` 가 예측한 오픈 시각 5분 전부터 폴링을 10초로 조이고, 열리는 순간
좌석 감시로 자동 전환. → `worker_open` / `worker_radar` 를 잇는 마지막 조각.

### (3) I. 진짜 취소 vs 결제 실패 반환 구분

좌석 선점이 풀린 것뿐이면 몇 분 내 다시 잡힘. 발견 후 60초 뒤 재확인해 살아있는
자리만 알리면 헛걸음이 줄어듦. → `worker_seat` 알림 직전에 확인 단계 추가.

### (4) H. 취소표 히트맵 → 폴링에 반영

`history.jsonl` 이 쌓이면 "요일 × 시간대별 취소 발생률" 을 뽑아 `Scheduler.interval_for`
가중치로 사용. A 의 완성형.

### (5) 헤드리스 모드 (상주 관측 실용화)

지금은 대화형 마법사뿐. `--radar --brand lotte --cinema 1016 --movie 오디세이 --once`
같은 인자를 받으면 Windows 작업 스케줄러로 10분마다 돌릴 수 있음.
→ 예측 정확도는 `openlog.jsonl` 축적량에 비례하므로 사실상 (2)(4)의 전제.

### 그 밖에 논의만 된 것

- D. 다중 프로필 동시 감시 / F. 여러 지점 동시
- E. 백그라운드 상주 + Windows 토스트 + 텔레그램 원격 제어
- G. 안전장치 (연속 N주기 대상 0건이면 조건·API 이상 경고)
- K. 매진 예측 / L. 명당 등급 알림 / M. 로컬 웹 대시보드

---

## 4. 새 세션에서 쓸 프롬프트

아래를 그대로 붙여넣으면 됩니다.

```
C:\...\cinema-watch 에서 이어서 작업할 거야. (경로는 클론한 위치로)

HANDOFF.md 와 README.md 를 먼저 읽고 현재 상태를 파악해줘.
cinema_watch.py 단일 파일에 3,100줄 정도 있고, 표준 라이브러리만 쓴다.

이번에 할 일: (아래에서 골라서 적기)
- CGV 상영표 API 붙이기 (Burp 캡처 준비됨 / 아직 안 됨)
- J. 오픈 임박 대기 모드
- I. 진짜 취소 판별
- H. 취소표 히트맵
- 헤드리스 모드 (--radar 인자)

주의: HANDOFF.md 의 "함정 모음" 에 적힌 것들은 이미 확인·수정된 사항이니
같은 실수 반복하지 말고, API 사실도 재조사하지 말 것.
작업 후에는 실제 API 로 검증하고 커밋해줘.
```

CGV 를 진행할 경우 캡처본을 함께 주면 됩니다:

```
CGV 앱 예매 화면 트래픽을 Burp 로 떴어. 상영표 요청은 이거야:
(요청 URL / 헤더 / 바디 붙여넣기)

이걸로 CgvProvider.showtimes() 를 구현하고 supports_shows 를 켜줘.
```

---

## 5. 커밋 규칙

- 커밋 메시지는 한국어, 무엇을·왜 중심. 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- 로컬 데이터 파일은 절대 커밋하지 말 것 (`.gitignore` 에 이미 있음).
- 저장소: `https://github.com/shionista/cinema-watch` (Private, main 브랜치).
