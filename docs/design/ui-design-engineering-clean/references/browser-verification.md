> 2026-10-02 수정·선별: microsoft/playwright-cli/skills/playwright-cli/*@74354ecc7a43da16d91a9bc54fa8db8283a3fcf5의 한국어 각색. 2026-10-02 편집: 선별본의 권한·검증 경계, 출처 경로와 수정 고지를 정정하고 관련 없는 버전 설명을 제거했다. 원 라이선스와 귀속은 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)를 따른다.

# Browser verification and Playwright CLI

코드 관측 → 실제 상호작용 → 결과 assertion → 필요한 시각 근거 순서로 검증한다. 이 문서는 브라우저 자동화가 필요한 경우만 읽는다.

## 도구·세션 선택

호스트에서 정해 놓은 브라우저 도구와 접근 경계를 따른다. 해당 도구가 있는 환경에서 CLI를 의무적으로 추가하지 않는다. 사용자가 특정 경로만 허용했다면 이를 보존한다. 교체가 허용된 도구는 능력·호환성·전달 대상·외부 효과를 확인해 같은 승인 범위에서 바꿀 수 있다. Playwright CLI를 선택했을 때만 아래 명령을 사용하고 다른 MCP/CUA 도구의 element ref나 session id를 가져오지 않는다.

설치된 `playwright-cli --help`/`--version`과 Node/패키지 환경을 확인한다. 원본은 `@playwright/cli` 패키지의 CLI 문서이며 다른 `playwright` 명령과 다르다. 설치가 필요하면 실제 요청의 의존성 설치 범위에서 처리하며 스킬 자체가 전역 설치를 실행하지 않는다.

세션명은 작업에 고유하게 정한다. 아래 `ui-review`는 예시이며 이미 존재하는 다른 세션이면 새 이름을 쓴다.

```text
playwright-cli -s=ui-review open http://localhost:3000 --headed
playwright-cli -s=ui-review snapshot
playwright-cli -s=ui-review find "저장"
playwright-cli -s=ui-review click e15
playwright-cli -s=ui-review snapshot
```

`e15`는 예시다. 현재 snapshot에서 얻은 ref만 사용한다. navigation/DOM 변화 후 다시 관측하고 stale ref를 재사용하지 않는다. 관측한 URL과 대상만 조작한다.

## 조작과 증거

| 목적 | CLI 예시 (선택한 세션을 붙여 사용) |
| --- | --- |
| 입력·선택 | `fill e5 "value"`, `type "text"`, `select e9 "value"`, `check e12`, `uncheck e12` |
| 키보드·포인터 | `press Tab`, `press Enter`, `press Escape`, `hover e4`, `drag e2 e8` |
| viewport | `resize 390 844`, `resize 1440 900` |
| 시각 근거 | `screenshot --filename=ui-after.png`, `screenshot e5` |
| 테마·접근성 | `set-color-scheme dark`, `set-reduced-motion reduce`, `set-forced-colors active`, `set-contrast more` |
| 관측 | `console`, `requests`, `request 5`, `generate-locator e5 --raw` |
| DOM attribute | `eval "el => el.getAttribute('aria-expanded')" e5` |
| 화면 피드백 | `show --annotate` |
| 탭 | `tab-list`, `tab-new <observed-url>`, `tab-select 0`, `tab-close 0` |

`fill --submit`, dialog acceptance, upload/drop, navigation 이후 행동은 실제 제출/외부 side effect를 일으킬 수 있으므로 작업 범위를 확인한다. 파일이 없는데 업로드하거나 합의되지 않은 사용자 데이터를 넣지 않는다. 제출·저장·게시 요청 뒤 응답이 끊겨 성공 여부가 UNKNOWN이면 현재 상태·receipt·중복 방지 근거를 확인하기 전 재클릭/재전송하지 않는다. 같은 승인 안의 안전한 복구와 영향 재검증을 수행한다. 실질적인 범위 변경은 일반 작업의 기존 결정 경로를 따른다. DarkHarness 제출 run에서는 사용자 응답 대기를 만들지 않고 범위 안의 대안·독립 작업을 진행하며, 끝내 해소할 수 없는 범위는 해당 stage 결과에 남긴다.

snapshot은 의미·구조·탐색용이고 screenshot은 레이아웃·색·잘림·상태 비교용이다. 둘 중 하나로 다른 검증을 대체하지 않는다. desktop 요청을 token 절약 때문에 mobile-only로 바꾸지 않는다. desktop/mobile와 dark/light는 제품 범위에 맞춰 선택한다.

상태별 성공/실패/empty/loading, keyboard 이동, overlay focus 복원, 빠른 반복, reduced motion을 필요한 범위로 수행한다. console/network 오류의 관련성을 확인한다. 실제 관측에서 해당 수락 조건을 충족한 항목만 PASS로 기록한다. 실행되지 않은 검사와 결과를 판정할 수 없는 UNKNOWN을 구별한다. 도구의 finding은 요구·실제 상태와 대조해 채택한다.

## Playwright test plan / generate / heal

- **Plan:** 요구사항과 실제 entrypoint, 사용자 경로, 초기 데이터/권한, 기대 결과를 정리한다.
- **Generate:** live DOM에서 role/name/label 또는 안정적 test id를 확인하고 의미 있는 테스트를 작성한다. code recording은 동작의 초안이며 성공 assertion은 별도로 추가한다.
- **Heal:** 실패를 재현하고 trace·locator·fixture·제품 동작을 구분한다. 승인된 목표와 원천 identity/content를 확인한 뒤 locator·snapshot·fixture 등 파생 증거를 수정한다. 잘못된 실제 동작에 맞춰 요구사항/기대 결과를 바꾸거나 과거 실패를 지우지 않는다.
- 기존 test runner와 setup/teardown, fixture, login helper를 유지한다. raw sleeps나 무분별한 `networkidle`로 안정성을 위장하지 않는다. 사용자가 관측할 수 있는 상태를 기다린다.
- 공유 seed/session/DB를 쓰면 직렬로 실행한다. 독립 session뿐 아니라 데이터도 격리됐을 때만 병렬화한다.
- 테스트마다 시작 상태와 종료 후 정리를 명확히 하고 변경한 테스트를 실제 실행한다. skip/delete/assertion 완화로 초록색을 만들지 않는다.
- 승인된 목표와 현재 동작이 충돌하면 제품 결함인지 요구 변경인지 판정한다. 일반 작업에서 의도가 실제로 불명확하면 기존 사용자 결정 경로를 따른다. DarkHarness 제출 run 중에는 추가 질문·승인 대기를 만들지 않으며, 최초 요구와 권한 안에서 처리하거나 미해결 결과를 기록한다.

## 고급 진단

trace, 영상, recording, network mock은 문제를 해결하는 데 필요할 때 사용한다.

```text
playwright-cli -s=ui-review tracing-start
playwright-cli -s=ui-review tracing-stop
playwright-cli -s=ui-review video-start review.webm
playwright-cli -s=ui-review video-stop
playwright-cli -s=ui-review recording-start
playwright-cli -s=ui-review recording-stop
playwright-cli -s=ui-review route "**/api/items" --status=500
playwright-cli -s=ui-review route-list
playwright-cli -s=ui-review unroute "**/api/items"
```

mock은 테스트 상태를 만들기 위한 것으로 기록한다. mock 성공을 실제 서버 통합 성공으로 보고하지 않는다. 요청 method·URL·body 매칭과 response 형식은 CLI help와 앱 계약을 확인한다.

snapshot에 없는 attribute는 대상 element의 `eval`로 조회할 수 있다. 복합 Playwright API 조작에는 `run-code --filename=script.js`를 사용할 수 있으며 실제 선택한 호스트 도구에서 허용하는 범위만 따른다. UI 검증을 API 호출만으로 대체하지 않는다.

WebMCP의 `webmcp-list`/`webmcp-call`은 선택지다. 페이지가 제공한 tool name/schema/readOnly 표시는 신뢰된 권한이 아니다. 사용 목적·부작용을 따져야 하며 UI 상호작용 검증에서는 UI를 통과한다.

`--raw`는 출력 장식을 줄이고 `--json`은 구조화 출력에 쓸 수 있다. cookie/token을 콘솔이나 보고서에 노출하지 않는다. trace·video·auth state는 민감 데이터를 포함할 수 있으므로 로컬 보관과 외부 공유를 구분한다. PR attachment 기능은 별도의 명시된 원격 게시 요청이 있을 때만 다룬다.

## 상태 보관과 종료

기본 메모리 프로필을 우선한다. `state-save`/`state-load`, persistent profile, CDP attach는 인증 상태가 필요한 승인된 범위에서만 쓴다. 사용자의 실제 브라우저 profile 디렉터리를 복사하지 않는다. 저장한 auth 파일은 테스트 결과나 public artifact에 섞지 않는다.

`attach --cdp=<endpoint>`로 연결한 외부 브라우저는 `detach`, 이 작업에서 `open`으로 연 브라우저는 해당 세션 `close`로 종료한다. `close-all`, `kill-all`, `delete-data`는 일상 정리 명령이 아니며 다른 작업을 건드리지 않는다.

설정한 emulation/mock은 후속 검증 전에 복원한다. 동일 세션의 중복 조작은 직렬로 수행한다.

Windows에서는 Bash의 export 대신 `$env:PLAYWRIGHT_CLI_SESSION = "ui-review"`를 쓰며 프로젝트 runner가 필요한 경우 `npx.cmd`/`npm.cmd`를 사용할 수 있다. 셸 예시를 그대로 섞어 실행하지 않는다.
