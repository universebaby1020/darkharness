> 2026-10-02 수정·선별: ibelick/ui-skills/skills/fixing-accessibility/SKILL.md@b1cc8e0073ac64b09b3d38cd604407aa20c2b7ad; emilkowalski/skills/skills/emil-design-eng/SKILL.md@85e8e2363b713506e1d5b6e07a0eb2da66be1bc3의 한국어 각색. 2026-10-02 편집: 선별본의 권한·검증 경계, 출처 경로와 수정 고지를 정정하고 관련 없는 버전 설명을 제거했다. 원 라이선스와 귀속은 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)를 따른다.

# Accessibility

범위: UI 구조와 실제 사용성. 자동 검사나 screenshot만으로 WCAG 전체 적합성을 선언하지 않는다. 특정 표준/등급 준수를 요구받으면 해당 버전의 공식 기준으로 별도 평가한다.

## 우선순위와 이름

진행을 막는 이름·키보드·포커스 문제를 먼저 해결한다. motion/미디어 문제도 실제 영향에 따라 중대할 수 있으므로 원본의 낮은 분류를 고정 적용하지 않는다.

- 모든 interactive control에 의미 있는 accessible name을 제공한다. visible label과 불필요하게 다른 aria-label을 붙이지 않는다.
- 아이콘 단독 버튼은 숨긴 label 텍스트, `aria-label` 또는 `aria-labelledby` 등으로 이름을 갖고, input/select/textarea는 실제 label 연결을 갖는다.
- 링크 문구는 이동 목적을 설명한다. 장식 SVG는 `aria-hidden="true"`, 장식 이미지는 빈 alt를 사용한다. 정보 이미지는 용도에 맞는 대체 텍스트를 제공한다.
- native button/link/input이 해결하는 의미를 불필요한 role/ARIA로 다시 만들지 않는다. button의 submit 여부는 form 문맥에 맞게 명시한다.

## 키보드와 focus

- 독립 controls는 자연스러운 Tab 순서로 접근한다. 양수 tabindex를 추가하지 않는다.
- tablist/menu/combobox 같은 composite widget은 해당 패턴의 roving tabindex 또는 `aria-activedescendant`와 arrow-key 동작을 따른다. 모든 내부 항목을 개별 Tab stop으로 만들지 않는다.
- focus indicator는 가려지지 않고 식별 가능해야 한다. outline 제거에는 검증된 대체 표시가 필요하다.
- hover로 제공되는 기능은 keyboard/touch로도 접근 가능해야 한다.
- modal dialog는 적절한 initial focus, modal 동안의 focus containment, 뒤쪽 비활성화, 닫을 때의 focus 복원을 확인한다. trigger가 사라졌으면 논리적으로 다음 위치를 선택한다.
- Escape와 outside click은 해당 overlay/primitive의 계약을 따른다. 보존해야 하는 작성 데이터나 취소 불가 작업의 정책을 임의로 바꾸지 않는다.
- 열고 닫을 때 불필요한 페이지 스크롤이나 focus jump가 생기지 않게 한다. non-modal popover에 modal focus trap을 무조건 적용하지 않는다.
- opacity만 0인 퇴장 요소가 여전히 Tab으로 접근되거나 클릭을 가로채지 않도록 visibility/inert/unmount와 focus lifecycle을 함께 처리한다.

## 구조와 폼

제목 계층·랜드마크·목록·테이블 구조를 의미에 맞춘다. 숫자 단계만 기계 검사해 문서를 재구성하지 않는다. 표 헤더는 실제 셀 관계를 표현한다.

label, helper text, error text는 각각 실제 id로 연결한다. 오류가 있을 때 `aria-invalid`를 설정하고 필요한 입력의 required 의미를 보존한다. 색 외에 오류/선택/비활성 상태를 알아볼 수 있는 단서를 준다.

```html
<label for="email">이메일</label>
<input id="email" name="email" type="email" required
       aria-invalid="true" aria-describedby="email-help email-error">
<p id="email-help">답변 받을 이메일 주소를 입력하세요.</p>
<p id="email-error">이메일 주소 형식을 확인해 주세요.</p>
```

이는 오류 상태 예시다. 최초 화면부터 무조건 invalid로 만들지 않는다. 오류가 바뀌는 시점에는 error summary/focus 이동 또는 적절한 live region으로 알리고 중복 낭독은 피한다.

disabled submit의 이유는 접근 가능한 설명으로 제공한다. disabled 요소에 hover tooltip만 달아 해결했다고 판단하지 않는다.

## 상태 알림과 미디어

- loading은 `aria-busy` 또는 적절한 status text로 표현한다. spin 속도나 motion만으로 기다림을 알리지 않는다.
- 긴급하지 않은 상태는 polite announcement를 고려하고 중요한 오류는 발생 시점에 적절하게 알린다. 모든 갱신에 assertive live region을 추가하지 않는다.
- toast는 중요한 오류·필수 조치의 유일한 보관 위치가 되어서는 안 된다. 알림의 시간·dismiss·focus 동작을 검증한다.
- expandable trigger의 expanded 상태와 대상 관계를 해당 native/ARIA 패턴에 맞춰 유지한다.
- 텍스트·아이콘·경계·focus의 필요한 대비를 실제 테마와 상태에서 확인한다. 투명도·blur 때문에 읽기가 어려워지지 않아야 한다.
- 음성이 있는 영상에는 해당 콘텐츠와 요구에 맞는 자막을 제공하고 자동 음성 재생을 피한다.
- reduced motion은 장식 이동·확대·bounce·stagger를 제거하거나 크게 줄인다. 정적인 상태를 항상 남기며 필요한 짧은 fade/color만 선택적으로 유지한다. 사용자가 모든 모션 제거를 요청했으면 그것을 따른다.

## 확인

관련 경로를 keyboard만으로 수행하고 focus 진입/복원, error 연결, accessible name, reduced motion, forced colors/contrast를 적절한 도구로 확인한다. screen reader 실검증 여부는 따로 기록한다. 시각 스크린샷, 접근성 snapshot, 자동 scan은 서로 다른 근거다.
