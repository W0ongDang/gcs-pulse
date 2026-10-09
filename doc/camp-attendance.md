# 영어캠프 출석 연동

학생은 기존 GCS Pulse 계정으로 QR 출석한다. 별도 관리자 웹페이지가 회차를 만들고 출석을 마감하며 학생용 QR 화면을 결과 화면으로 전환한다. 원본 명단과 출석 기록은 이 서버에 저장한다.

## 배포

1. 서버와 클라이언트를 함께 배포한다. 기존 `python scripts/migrate_and_seed.py`가 `Base.metadata.create_all`로 새 `attendance_camp_*` 테이블을 생성한다. 기존 테이블 변경은 없다.
2. 관리자 웹페이지에 `GCS_API_URL`(이 서버), `GCS_CLIENT_URL`(GCS 클라이언트)을 함께 설정한다. `/camp/check-in/[token]` 주소는 서버의 기존 `AUTH_SUCCESS_URL`을 기준으로 만든다. 두 클라이언트 주소는 같아야 한다.
3. GCS·교수·admin 권한의 관리자 계정에서 기존 API 토큰을 발급한다. 별도 관리자 페이지에 토큰을 입력한다. 해당 페이지 서버만 토큰을 받아 GCS API를 호출하며 학생 화면에는 노출하지 않는다. 토큰 취소와 권한 변경은 다음 요청부터 적용된다.
4. `POST /camp-attendance/camps`에 이름과 학생 명단을 전달한다. `GET /camp-attendance/users`가 실제 GCS 사용자 ID와 현재 팀 이름을 제공한다. 소속이 다른 의미인 경우 관리자가 소속을 지정한다.
5. 반환된 캠프 ID를 관리자 페이지의 `GCS_CAMP_ID`로 설정한다. 공개 조회는 명단 전체·이메일·학번을 포함하지 않는다. 공개 결과는 출석자 이름·소속·1등 출석 시각과 순위만 제공한다.
6. 한 회차로 로그인 복귀 → QR 출석 → 관리자 마감 → 결과 화면 → 엑셀을 점검한 뒤 운영한다. 실제 Google OAuth·운영 PostgreSQL 검증은 배포 환경에서 진행한다.

## API

모든 경로는 `/camp-attendance` 아래에 있다. 관리자 API는 기존 세션+CSRF 또는 기존 Bearer API 토큰을 사용한다. 학생은 GCS 화면의 기존 세션과 CSRF를 사용한다.

| 메서드 | 경로 | 역할 |
|---|---|---|
| GET | `/identity` | 관리자 권한 확인 |
| GET | `/users` | GCS 사용자 후보 조회 |
| POST | `/camps` | 캠프 생성: `{name, members:[{user_id, affiliation}]}` |
| PUT | `/camps/{id}/members` | 캠프 명단 변경; 제외 학생의 과거 기록 보존 |
| GET | `/camps/{id}/admin` | 회차·명단·원본 출석·정정 기록·집계 |
| POST | `/camps/{id}/sessions` | 회차 추가: `{date:"YYYY-MM-DD", title}` |
| PUT | `/camps/{id}/sessions/{sid}/status` | `{status:"open"}` 또는 `{status:"closed"}` |
| GET | `/camps/{id}/public` | 현재 회차 상태와 마감 결과; `session_id`로 이전 마감 결과 조회 가능 |
| POST | `/check-in/{token}` | 로그인한 본인 출석. 처음은 `success`, 중복은 `duplicate` |
| POST | `/camps/{id}/sessions/{sid}/corrections` | `{user_id, action:"add" 또는 "cancel", reason}` |

오늘 날짜는 한국 시간 자정 기준이다. 같은 캠프·날짜에는 하나의 회차만 생성한다. 예정 회차는 당일에만 열 수 있다. 마감 회차는 다시 열지 않으며 관리자 정정은 가능하다. 지난 날짜의 열려 있는 QR도 출석을 차단한다. 다음 날짜의 회차는 관리자가 추가·개방한다.

캠프 행의 쓰기 잠금을 먼저 얻어 출석·마감·정정을 직렬화하고, 출석과 마감의 동시 요청을 처리한다. DB의 `(session_id,user_id)` 고유 제약으로 중복을 추가 방지한다. 서버 UTC 출석 시각과 기록 ID 순으로 첫 QR 출석자를 결정한다. 취소한 QR 출석의 복구는 수동 추가로 처리되어 1등 집계에서 제외된다.

마감 결과는 해당 회차까지의 마감 회차만 집계한다. 전체 평균에는 선택 회차를 포함한다. 1등 횟수, 총 출석횟수 순으로 정렬하고 두 값 동률은 공동 순위다. 개인 참여율은 출석한 마감 회차 수 / 전체 마감 회차 수다. 신규 참여는 유효한 출석을 한 가장 이른 교육 날짜 기준이다.

## 검증

`python -m pip install -r requirements-dev.txt` 이후 `ENVIRONMENT=test python -m pytest tests/test_camp_attendance.py -q`.

SQLite로 실제 인증·CSRF·DB 제약·동시 요청·마감·정정·공개 데이터·참여율을 검증한다. 실제 OAuth 리디렉션, 운영 DB의 행 잠금, 개인정보 공개 정책과 공개 화면의 접근 정책은 운영 담당자가 배포 전에 확인한다.
