# 외부 UIQA checker (WO-DH0-02R3 E)

Core 변경 없이 기존 `dh_verify` → VerificationBridge/Broker → `json-criteria-v1` → 단일 continuation → `dh_verification_read`를 사용한다. 이 문서는 새 제품 상태 머신이나 추가 권한을 정의하지 않는다.

## Controller 등록

`tools/uiqa/controller.py:configuration()`으로 고정 check 설정을 만든 뒤 **run 전에** 인증된 Grant의 `scope.verification.checks[check_id]`에 등록한다. 모델은 check_id, 정확한 peer revision, snapshot receipt만 전달한다. Python 실행 파일과 checker source의 Git HEAD/tree는 기존 broker가 고정·재검증한다. source는 `.git` 디렉터리가 직접 있는 독립 clone이어야 한다(worktree `.git` 파일은 기존 broker에서 거절). `{checkout}`와 `{output}`은 각각 별도 argv 항목이다. shell, 모델 plugin, eval, 임의 profile subprocess를 사용하지 않는다.

- 일반 UI: `effects.docker=true, network=true` (Docker build 중 의존성 다운로드 가능).
- 명세 기반 API-only: Docker label 부재 확인만 수행하므로 `docker=true, network=false`. 앱/브라우저 실행 안 함, N/A 보고는 UI PASS가 아님.
- 실행/로그 시간 제한을 추가하지 않는다. Playwright 1.63.0의 수정하지 않은 API 기본 timeout을 사용한다. 호출 동작이 오래 걸리는 것은 모델 턴 timeout과 별개다. controller가 추가 broker timeout을 필요로 한다면 사용자의 별도 결정을 받아야 한다.

## 격리와 발행 순서

1. host가 정확한 `{checkout}/stage-N`을 Docker build한다. 이미지 ID와 unique effect label을 기록한다.
2. `--internal` network에서 앱을 시작한다. 공식 runner는 고정 ID `sha256:6c82d0825613ef894eb2680ee61ffa23c5d0de6a8857525590b98c9d9663cf76`로 호출한다. tag/latest fallback이나 실행 중 tool 설치는 없다.
3. checker directory, **개별** committed JSON flow, fixed axe asset을 읽기 전용 mount한다. private scratch만 쓰기 가능한 bind mount이며 `{output}`은 절대 mount하지 않는다. runner는 read-only root와 no-new-privileges를 유지하며 공식 이미지의 기본 사용자(root), HOME, 기본 capability를 사용한다. worker는 신뢰된 CLI의 정확한 `/scratch`만 대상으로 전체 regular-tree 검사 뒤 루트와 자식을 현재 컨테이너 UID/GID로 넘긴다. `finally`에서 재검사하고 자식→루트 순서로 controller가 전달한 host uid/gid에 반환한다(기존 private mode 유지, 0777 완화·host sudo 없음). 최초 구현의 host uid/gid 강제·scratch HOME·cap-drop ALL은 공식 root-local browser 설치 접근 및 private scratch 읽기/소유권 반환과 충돌하므로 제거했다. Docker build 자체는 공식 방식과 같은 host-authorized 실행이며 악성 Dockerfile/같은 UID에 대한 OS 보안 경계라고 주장하지 않는다.
4. ERROR placeholder는 private scratch에만 만든다. raw axe/browser 로그, PNG, trace ZIP도 private scratch에 둔다. 페이지의 다른 origin 요청은 차단하며, 페이지/flow로 host command를 실행하지 않는다. JSON flow는 CSS selector와 제한된 Playwright action/assertion 데이터다.
5. 모든 해당 label의 container/network/app image를 제거하고 Docker daemon 조회가 성공하며 결과가 모두 비었는지 확인한다. Docker build cache까지 제거했다는 주장은 하지 않는다. 별도 run/effect와 공식 runner image는 제거하지 않는다.
6. host가 scratch의 symlink/ancestor symlink/hardlink/특수 파일을 거절하고 artifact SHA256을 다시 검증한다. check ID/status/schema, runtime tool pins를 검사하고 **count를 다시 계산**한다. 확인된 정리 뒤에만 선언된 `uiqa.json`을 host가 atomic replace로 발행한다. 출력 및 private 파일은 비공개다.
7. 정리 불명/unsafe scratch이면 선언 보고서를 발행하지 않는다. 일반 process exit에서 기존 broker는 UNKNOWN/fence로 유지한다. known report + nonzero는 known FAILED, exit0 COMPLETED에도 findings가 있으면 criteria FAILED다.

## 보고와 기준

보고서: SUT revision, checker version/hash, runner pin과 실제 관측 ID(미관측은 null), app image ID, Playwright/Chromium/axe 버전, axe SHA256/source/MPL-2.0, flow path/hash/author/spec reference, 실제 사용 viewport, check 결과/후보 finding, screenshot/trace/private raw artifact SHA256와 경로, execution(COMPLETED/ERROR), cleanup proof/effect label.

`criteria()`는 COMPLETED + cleanup.verified, applicable/total >0, failed/partial/errors=0, passed=applicable을 요구한다. **혼합 N/A + 적용 PASS는 허용**한다. 빈/all-N/A는 PASS가 아니다. host는 summary를 신뢰하지 않고 check들로 다시 계산한다. axe incomplete는 violation과 분리하여 PARTIAL로 보존한다. document overflow는 의도된 지역 스크롤과 구분하여 Reviewer가 재판정할 **후보**다. 자동 scan 결과가 전체 WCAG/시각 QA 통과를 의미하지 않는다. screenshot을 생성했다는 것만으로 이미지를 봤다고 주장하지 않는다(UIQ-T19 PARTIAL).

## Flow와 development fixture

일반 run flow는 Reviewer가 명세에서만 작성하고 Builder가 지시받은 generic repo 경로에 commit한다. shipped test에서 역산하지 않는다. 작성자는 room 기록으로 남긴다. 그 경로가 실제로 막힌 근거는 관측되지 않았으므로 Core placeholder 변경은 하지 않았다.

`tools/uiqa/fixtures/toy-stage-2.flow.json`은 **개발용 controller fixture**다. 공식 `toy/spec/stage-2.md` L7–19의 exact data-testid, increment/reload만 사용한다. stage-1 L13의 default port 8080을 따른다. 지정하지 않은 화면 폭 375/1280은 developer sampling이며 toy 명세의 강제조건이 아니다. Band Reviewer 작성 flow 또는 실제 run 성공 근거가 아니다. API-only는 stage-1 L10의 근거를 별도로 기록한다.

허용 action: click/fill/press/visible/text/focused/integer/integer_delta/reload. integer는 selector의 현재 정수 baseline을 기억하고 integer_delta는 같은 scenario의 그 baseline에서 명시 delta만큼 바뀐 text를 확인한다. 페이지 내용을 executable로 해석하지 않는다.

## axe asset

`export_axe.py`는 Main이 새 사용자 로컬 경로로 실행하는 fixed **axe-core 4.10.3** export helper다. 공식 npm registry metadata/tarball integrity를 확인하고 axe.min.js, 원문 LICENSE(MPL-2.0), package.json과 manifest의 version/source/SHA256을 내보낸다. tar 전체 extract, npm/global 설치, `@latest`, browser binaries commit은 하지 않는다.

Main이 이미 사용자 로컬의 `<USER_LOCAL_AXE_DIR>`에 고정 axe-core 4.10.3을 export했다. 이 표기는 실제 경로를 공개하지 않는 문서용 placeholder이며, 실제 자산 경로는 controller 설정으로 전달한다. 재다운로드하지 않는다. 확인한 axe SHA256: `880970c081707360e64f34cea25ff91892f5bc95675b0776925b9709dd8a68bb`. 이 경로/manifest는 공개 repository에 복사하지 않는다.

## Main 실제 controller 시험

정확한 준비 명령은 `evidence/wo-dh0-02r3-uiqa/API_HANDOFF.md`에 있다. Main만 source commit/freeze/direct clone과 실제 Docker 시험을 실행한다. 이 worker는 fake component tests만 실행한다.

`run_controller_tests.py`는 기존 완료 result를 read-only input으로 읽고 별도 사본에서만 fixture flow/음성 control을 commit한다. real LocalGitBroker origin + independent snapshot → real VerificationBridge/Broker subprocess → guarded read + continuation 1회 + UNKNOWN fence 부재를 검사한다. in-memory serialized owner는 fixture이며 live room/SDK/model 시험이 아니다. 실제 Docker/브라우저는 모형이 아니다.

양성은 **tool path 통합**이다: COMPLETED report, applicable>0, 정확한 revision/tool pins, cleanup verified, continuation1/fence0이면 앱에 진짜 findings가 남아 broker가 known FAILED여도 통합 경로 성공이다. `sut_criteria_accepted`는 별도 기록하고 실패를 숨기거나 original app을 고쳐 성공으로 만들지 않는다. Missing axe/startup failure는 ERROR+knownFAILED, stage-1 API N/A는 UI PASS 아님, 주입 음성 copy는 overflow/axe violation 발견을 엄격히 요구한다. report와 raw 결과를 읽고 판단하며 script exit만으로 승인하지 않는다.

## STOP / SIGKILL 한계 (PARTIAL)

host는 TERM/INT 시 정리를 시도한다. 하지만 **기존 broker는 cancel/revoke에서 SIGKILL을 사용하고 report 검증 없이 CANCELLED를 기록**한다. 따라서 SIGKILL된 host가 정리를 수행했다고 주장할 수 없고, cancel 경로가 반드시 UNKNOWN fence로 바뀐다고도 주장할 수 없다. 이 gap은 기존 코드 읽기로 확인했으며 실제 SIGKILL/Docker 재현은 Main 미실행이다. Core patch는 이 범위에서 하지 않았다.

private `effect.json`의 정확한 ID로 Main이 다음을 실행한다:

```
python3 -B tools/uiqa/cleanup_owned.py EFFECT_ID NEW_PRIVATE_CLEANUP_DIRECTORY
```

사후 정리 결과는 원래 실행을 PASS로 승격하거나 자동 재실행하는 근거가 아니다. STOP continuation은 만들지 않으며 기존 정산 절차와 사용자 취소 의미를 유지한다. 실제 controller STOP 검증은 PARTIAL/남은 과제로 전달한다.

## 문안·권리·최종 검사 경계

영어 dispatch addendum: `tools/uiqa/GENERIC_UI_QUALITY_ADDENDUM_EN.md`. 완전한 task/spec와 매 handoff에 다시 붙인다. generic mandate/design pack/notices는 hygiene/Main 소유이므로 이 worker가 중복 편집하지 않는다. 147개 원 입력 판정표는 evidence에 byte 그대로 보존한다. UIQ-T01..T36은 실제 수준별 표로 전달하며 WAIVED는 없다. full suite/live readiness/전체 notices 확인은 Main 최종 freeze 뒤 한 번의 통합 검사에 속한다.

## 2026-10-02 runtime repair

음성 control은 기존 toy Dockerfile의 Python base를 그대로 두고 HTML/CMD를 추가한다. `FROM sha256:<config ID>`는 BuildKit이 registry 이름으로 해석하므로 쓰지 않는다. 브라우저 phase의 공식 runner ID pin은 바뀌지 않는다.

ERROR report와 private `host-diagnostic.json`/`browser-diagnostic.json`에는 고정된 phase와 허용된 exception class만 남긴다. 예외 문자열·DOM·URL·원문 stack은 보고하지 않는다. 양성 실행의 옛 예외가 삭제되어 사후 확정되지 않는 한계를 보존한다. 이미지 읽기 전용 검사에서 root-local browser 설치와 0700 `/root`를 확인했으나 repaired runtime 성공은 Main의 새 여섯 case로 판단한다.

새 freeze/재시험 명령은 `evidence/wo-dh0-02r3-uiqa/repair/API_HANDOFF.md`에 있다. 기존 첫-pass evidence·147개 표·UIQ-T01..T36·영어 addendum은 변경하지 않았다.

Startup 음성 시험은 generic ERROR만으로 인정하지 않는다. 앱의 curated running/status/exit_code(health 로그 제외)를 시작 직후와 브라우저 종료 뒤에 관측한다. `/bin/false` 주입의 exited/exit1과 app_startup_state 또는 navigation 실패를 함께 요구한다. tool_import/browser_launch/axe_scan 오류는 startup 주입의 증거로 세지 않는다. missing axe는 axe_validation/FileNotFoundError, 주입 control은 COMPLETED/exit0와 실제 해당 finding을 요구한다. 관측은 단일 daemon 조회이며 새 retry/timeout/readiness 정책을 넣지 않는다. 최초 Main raw pass boolean은 변경하지 않는다.

### scratch ownership 후속 수정

Main이 전달한 `UIQA_BROWSER_NATIVE_DIAGNOSTIC.json`과 `UIQA_SCRATCH_OWNER_DIAGNOSTIC.json`의 관측 원인은 `/scratch/tmp` 쓰기/탐색 권한이다. 같은 공식 runner·flow·flags에서 scratch 루트와 자식의 소유권 전달 후 return0/cleanup verified를 관측했다는 전달 사실이며, 이 worker의 실제 Docker 재현은 아니다. font-cache 경고를 확정 원인으로 승격하지 않는다.

`browser_worker.main()`은 `/scratch`의 링크(ancestor 포함)·특수 inode·하드링크를 소유권 변경/진단 쓰기 전에 거절한다. 부분 handoff 실패, flow 실패, 진단 쓰기 실패에도 소유권 반환을 시도하며, 반환 검증/변경 실패는 성공 종료가 아니다. 새로운 mount/image/user/HOME/capability/security/timeout/routing 변경은 없고 trace/axe 검사도 유지한다. SIGKILL에서 `finally` 실행과 소유권 반환은 보장되지 않으므로 기존 STOP PARTIAL 및 선언 보고서 no-fake 규칙은 그대로다.

Linux fake 집중 시험, 고정 source/tests/docs 해시 및 Main 재시험 handoff는 `evidence/wo-dh0-02r3-uiqa-scratch-owner/`에 둔다. 소유권 syscall은 모형이며 DrvFS의 FIFO 미지원 때문에 특수 inode type도 모형으로 검사한다. 실제 Docker 여섯 case·full suite·production seal은 여기서 주장하지 않는다.
