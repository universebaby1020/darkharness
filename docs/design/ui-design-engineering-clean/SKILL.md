---
name: ui-design-engineering-clean
description: 웹 UI의 시각 위계, 상태·피드백, 접근성, 모션과 실제 브라우저 검증을 다룬다. 기존 제품 계약과 권한을 보존하며 필요한 자료만 읽는다. 네이티브 전용 GUI나 API-only 작업에 웹 검사를 강제하지 않는다.
metadata:
  revision: "3-rights-pruned-r1"
---

# UI Design Engineering — 권리 위험 축소 선별본

사용자 제공 v2와 일반적인 R2/R3 디자인 개념을 토대로, 이번 사용자 요청에 따라 위험 원자료와 제한된 파생분을 제외한 편집본이다. 법적 무위험 인증서가 아니다. [고지](THIRD_PARTY_NOTICES.md)와 [출처·제외 경계](SOURCES_AND_SCOPE.md)를 함께 유지한다.

## 의미와 권한

[사용자 공통철학](governance/COMMON_PRINCIPLES_KO.txt)과 [공통 지침 발췌](governance/COMMON_GUIDANCE_SCOPE_KO.md)를 적용한다. 최신 유효 사용자 결정과 실제 강제 제약이 우선하며, 이 스킬은 새로운 승인 단계·기능·기본값·정책을 발급하지 않는다. 이미 문맥에 있는 유효 기준을 매번 중복 통독하게 하지 않는다.

기존 화면·토큰·코드·검수 자료는 현재 상태의 증거이지 사용자 확정 결정이 아니다. 수치 예시·권장안·기본값·강제조건을 구분한다. 승인 범위 안의 가역적 구현 선택과 검증은 수행하고, 실제로 달라지는 의미·권한·비용·외부효과를 임의 확정하지 않는다.

## 흐름

1. 요구·지원 범위·기존 구성·실행 환경을 확인한다. 명세에 없는 기능/프레임워크/테마를 발명하지 않는다.
2. 필요한 상태·상호작용을 관측하고 의도와 현재 상태를 분리한다. UI가 없는 단계에는 미래 screenshot을 요구하지 않는다.
3. 승인된 구현은 수행하고, read-only review 요청은 finding 생성 범위로 지킨다. 작업 소유자와 수정 권한을 혼동하지 않는다.
4. 실제 브라우저 동작과 필요한 시각 근거로 확인한다. 자동 도구 결과는 원문 요구·실제 상태와 대조한다.
5. 같은 승인 안에서 검사·전달·경로·세션·fixture 결함을 수리하고 영향 범위를 재검증한다. 안전 차단은 해당 원인을 해소한 뒤 재개한다.
6. 수행 수준, PASS/FAILED/UNKNOWN, NOT_RUN, PARTIAL/COMPLETE, 권한 상태를 분리해 보고한다. 다른 task와 무관한 blocker를 전파하지 않는다.

## 필요한 자료만 선택

| 주제 | 문서 |
|---|---|
| 위계·타이포·표면·정렬·아이콘·조작 영역 | [visual-craft.md](references/visual-craft.md) |
| 키보드·focus·label·semantics·오류·접근성 | [accessibility.md](references/accessibility.md) |
| 상태·레이아웃의 일반 디자인 검토 | [layout-and-state.md](references/layout-and-state.md) |
| 모션 의미·중단·reduced motion | [motion.md](references/motion.md) |
| 브라우저·증거·격리·재현 | [browser-verification.md](references/browser-verification.md) |
| shadcn을 이미 사용하는 경우만 | [shadcn.md](references/shadcn.md) |

스킬을 읽는 것이 도구 설치/전역 plugin/추가 모델 호출 권한은 아니다. 기존 런타임으로 연결하고 실제로 로드됐는지 확인한다. 명령 예시는 실행 권한이나 최신 호환성 보증이 아니다. 선택한 버전의 실제 도움말/API가 필요하다.

## 품질과 판정

색·그림자·애니메이션보다 현재 상태의 사실성과 실제 기능·가독성·조작 가능성을 먼저 확인한다. 이것이 특정 취향을 전역 금지하거나 “최소한 아무 모양”을 최종 품질로 인정한다는 뜻은 아니다. 현재 요청 범위에서 일관되고 정돈된 화면으로 구현한다.

finding은 후보이며 ACCEPT/MODIFY/REJECT/UNRESOLVED로 근거를 남길 수 있다. 검사 실패를 숨기려고 baseline·assertion·명세를 바꾸지 않는다. 반대로 표현 취향이나 비적용 표준을 blocker로 올리지 않는다. 숫자 score/검사 건수/스크린샷 hash는 품질 전체의 증거가 아니다.

이 스킬은 승인·대기·복구 정책을 소유하지 않는다. DarkHarness 제출 run에서는 최초 지시 이후 사용자 질문·승인·resume을 추가하지 않는 현재 계약을 보존한다. unresolved 필수 요구를 가짜 PASS로 바꾸지 않고 허용된 대안/내부 수리를 먼저 수행한다.
