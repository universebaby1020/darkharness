> 2026-10-02 수정·선별: jakubkrehel/make-interfaces-feel-better/skills/make-interfaces-feel-better/*@35545ea1512ad59fa463e6b1f95ca9c052981fe6; emilkowalski/skills/skills/emil-design-eng/SKILL.md@85e8e2363b713506e1d5b6e07a0eb2da66be1bc3; shadcn-ui/ui/skills/shadcn/*@98a1fe67b439324ddc857f47fbdce056600a4329의 한국어 각색. 2026-10-02 편집: 선별본의 권한·검증 경계, 출처 경로와 수정 고지를 정정하고 관련 없는 버전 설명을 제거했다. 원 라이선스와 귀속은 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)를 따른다.

# Visual craft

범위: 타이포그래피, 표면, 정렬, 아이콘, 터치 영역. 인터랙션의 의미와 접근성은 [accessibility.md](accessibility.md), 모션은 [motion.md](motion.md)를 따른다.

## 기존 체계를 기준으로 관측

가까운 컴포넌트와 공유 토큰에서 현재 패턴을 관측하고, 최신 유효 요구·승인된 디자인과 대조한다. 기존 구현만으로 의도를 확정하지 않는다. 파일 하나의 취향에 맞추려고 프로젝트 폰트, 아이콘 세트, 스타일링 라이브러리를 교체하지 않는다. 모든 테마와 대상 viewport에 필요한 상태를 확인한다.

## 타이포그래피

- 짧은 제목의 고아 단어와 줄 길이 불균형에는 `text-wrap: balance`를 검토한다. 본문·설명에는 지원 브라우저에서 `pretty`가 도움이 되는지 확인한다. 긴 글, 코드, 의도된 개행에는 일괄 적용하지 않는다.
- 카운터·타이머·가격 갱신·숫자 표 열에는 `font-variant-numeric: tabular-nums`를 검토한다. 글꼴 지원과 실제 숫자 폭을 확인한다.
- 폰트 smoothing은 OS/폰트에 따른 시각 선택이다. macOS에서 검토할 수 있으나 모든 OS의 개선이나 대비 향상을 보장하지 않는다. 수정 범위를 넘어 root에 강제하지 않는다.
- 기존 font family와 대체 글꼴을 유지한다. 새 유료 폰트를 polish의 전제조건으로 만들지 않는다.
- 번역 문자열·긴 이름·확대·줄바꿈으로 버튼이나 필수 정보가 잘리지 않는지 확인한다. 말줄임은 전체 정보를 접근 가능한 방식으로 확인할 수 있을 때 쓴다.

## 표면과 정렬

- 가까이 맞물리는 둥근 표면은 `outer radius ≈ inner radius + gap`을 출발점으로 본다. border 두께·실제 inset·디자인 토큰을 포함해 판단한다. 간격이 넓거나 별개 표면이면 독립 반경이 자연스러울 수 있다.
- 시각적으로 치우친 삼각형·비대칭 아이콘은 실제 렌더 크기에서 optical alignment를 조정한다. 아이콘 쪽 패딩을 조금 줄이거나 SVG viewBox를 조정할 수 있지만 `2px`을 보편 규칙으로 삼지 않는다.
- 깊이를 표현하려는 경계에는 약한 layered shadow를 검토한다. 구분선, 입력 경계, 선택 상태, focus indicator는 제거하지 않는다. forced-colors에서도 구조와 포커스가 남아야 한다.
- 이미지가 배경에 묻히면 레이아웃 크기를 바꾸지 않는 얇은 inset outline을 고려한다. 중립색 black/white 저불투명도는 예시다. 기존 semantic token·브랜드·테마와 대비 요구를 우선한다. 투명 이미지나 모든 이미지에 일괄 테두리를 만들지 않는다.
- hover/pressed/disabled/loading 변화로 폭·높이·주변 정렬이 불필요하게 흔들리지 않게 한다.

## 아이콘

- 프로젝트의 동일 아이콘 라이브러리와 native grid를 사용한다. 16/20/24px 등 실제 표시 크기에서 획과 단순성을 확인한다.
- 인접 텍스트와 optical weight를 맞춘다. 24px grid의 1.5/2/2.5px 획은 예시이며 아이콘 디자인과 component API를 우선한다.
- 가능한 SVG `currentColor`를 사용해 state 색을 CSS로 관리한다. 브랜드 다색 artwork는 무조건 단색화하지 않는다.
- outline/default와 fill/selected는 일관된 상태 구분을 만들 때만 적용한다. label, `aria-pressed`, 선택 표시 등 비모션 단서도 제공한다.
- 아이콘 단독 버튼에는 accessible name을 준다. 장식 아이콘은 접근성 트리에서 숨긴다. crossfade용 복제 아이콘이 이름을 중복 낭독하지 않게 한다.
- RTL에서는 의미가 방향에 의존하는 back/forward·indent 화살표를 검토한다. 로고·체크·시계·미디어 재생 아이콘을 통째로 뒤집지 않는다. composite glyph는 부분별 의미를 확인한다.
- shadcn 내부 아이콘은 해당 버전이 제공하는 `data-icon`과 component sizing을 우선하고 불필요한 크기 클래스를 덮어쓰지 않는다.

## 조작 영역

터치에서 약 44×44 CSS px, 밀도 높은 desktop에서 약 40×40은 이 소스들의 디자인 권장값이다. 이를 WCAG의 보편적 최소치나 합격 증명으로 부르지 않는다. 명시된 플랫폼/접근성 기준은 별도로 확인한다.

작은 visual glyph에는 padding, label wrapper 등 실제 hit area를 확보한다. pseudo-element 확장은 클릭·포커스 경계와 인접 타깃 충돌을 실제로 확인할 때만 쓴다. native input의 pseudo-element 렌더링을 가정하지 않는다. 영역이 겹치면 단순히 다른 타깃을 덮지 말고 spacing 또는 layout을 조정한다.

hover 효과는 `(hover: hover) and (pointer: fine)`에서 제공할 수 있으나 동작 자체를 hover에만 의존시키지 않는다. keyboard focus와 touch에서 동일 기능이 가능해야 한다.
