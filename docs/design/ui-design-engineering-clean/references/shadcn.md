> 2026-10-02 수정·선별: shadcn-ui/ui/skills/shadcn/*@98a1fe67b439324ddc857f47fbdce056600a4329의 한국어 각색. 2026-10-02 편집: 선별본의 권한·검증 경계, 출처 경로와 수정 고지를 정정하고 관련 없는 버전 설명을 제거했다. 원 라이선스와 귀속은 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)를 따른다. 명령은 호환성 확인 전 실행하지 않는 예시이며 `<PINNED_VERSION>`은 기존 환경에서 결속할 자리다.

# shadcn/ui

shadcn/ui 사용이 확인되거나 사용자가 이를 선택한 작업에만 적용한다. 다른 UI 체계를 shadcn으로 자동 전환하지 않는다. 설치된 컴포넌트 API는 현재 호환성의 근거이며 의도된 제품 동작이나 영구 보존 요구를 증명하지 않는다. 승인된 목표와 대조하고 필요한 업데이트는 그 승인 범위에서 수행한다.

## 프로젝트 파악과 문서

`components.json`, package manifest/lockfile, 실제 UI 경로를 먼저 확인한다. 원본의 셸 interpolation이 실행되어 context가 이미 주입됐다고 가정하지 않는다.

```text
npx shadcn@<PINNED_VERSION> info --json
npx shadcn@<PINNED_VERSION> docs button dialog select
npx shadcn@<PINNED_VERSION> search @shadcn -q "sidebar"
npx shadcn@<PINNED_VERSION> view @shadcn/button
```

프로젝트에 맞는 runner(`npx`, `pnpm dlx`, `bunx`)와 검증한 버전을 사용한다. `docs` 결과는 URL이므로 실제 내용과 해당 버전 API를 확인한다. CLI를 못 쓰면 로컬 컴포넌트/버전에 맞는 공식 문서를 활용하고 미확인 기능을 추정하지 않는다.

context에서 실제 aliases, resolvedPaths, base, framework, isRSC, packageManager, iconLibrary, tailwindVersion, tailwindCssFile, style, preset, installed components를 확인한다. `@/components/ui`, Lucide, Radix, Next.js를 하드코딩하지 않는다. `use client`는 RSC의 적절한 client boundary에만 둔다.

## 스타일과 구성

- 설치된 primitive와 variant/size를 우선 조합한다. semantic color token을 사용하고 theme 변경은 기존 token layer에서 한다.
- component `className`은 주로 layout에 쓰며 임의 color/typography 덮어쓰기로 공유 variant를 무너뜨리지 않는다. 명시된 디자인 변경은 적절한 token/variant로 구현할 수 있다.
- Tailwind 프로젝트에서는 gap, 동일 가로세로 size, truncate, cn 유틸을 기존 지원 버전에 맞게 쓴다. 유효한 기존 space-x/y 문법을 기능 결함으로 판정하거나 전체 변환하지 않는다.
- overlay stacking은 primitive/portal 정책을 확인한다. 겹침 문제가 있다고 임의의 z-index를 연쇄 추가하지 않는다.
- 장식 이미지 경계도 theme token으로 표현할 수 있으며 neutral-black/white 강제와 semantic token 강제를 동시에 적용하지 않는다.
- iconLibrary와 component icon sizing을 따른다. 해당 버전이 지원하면 `data-icon="inline-start"`/`"inline-end"`를 활용하고 아이콘 객체를 전달한다.

## 컴포넌트 선택

| 필요 | 기존 구현에서 확인할 후보 |
| --- | --- |
| 폼 | FieldGroup, Field, Input, Select, Combobox, Textarea, Checkbox, RadioGroup, Switch |
| 입력 부가 요소 | InputGroup, InputGroupInput/InputGroupTextarea, InputGroupAddon |
| 토글·선택 | ToggleGroup, RadioGroup, Select — 선택 의미와 키보드 패턴으로 결정 |
| overlay | Dialog, Sheet, Drawer, AlertDialog, Popover |
| 피드백 | Alert, Empty, Progress, Skeleton, Spinner, 프로젝트 toast |
| 구조·탐색 | Card, Tabs, Sidebar, NavigationMenu, Breadcrumb, Pagination, Separator |
| 데이터·콘텐츠 | Table, Badge, Avatar, Chart, ScrollArea, Accordion |
| 채팅 | 설치된 경우 MessageScroller, Message, Bubble, Attachment, Marker |

이름이 원본에 있다고 프로젝트에 존재한다고 가정하지 않는다. 설치된 대안과 API를 확인한다. 2–7개 선택지라는 개수만으로 radio/select를 ToggleGroup으로 바꾸지 않는다.

## 접근성과 composition

- label은 control id와 연결하고 설명/오류는 aria-describedby로 연결한다. 지원 버전에서는 Field의 data-invalid/data-disabled와 control의 aria-invalid/disabled를 맞춘다.
- FieldSet/FieldLegend는 실제 관련 checkbox/radio group에 사용한다.
- InputGroup에는 대응 input primitive를 사용하고 필요한 addon을 조합한다.
- Dialog/Sheet/Drawer는 이름이 있는 title을 갖는다. 시각적으로 숨겨도 접근성 이름은 남긴다.
- TabsTrigger는 TabsList 문맥에, 메뉴/선택 item은 해당 API가 요구하는 group/content 문맥에 둔다. 현재 로컬 wrapper의 동작과 승인된 계약을 구별해 확인한다.
- Card의 header/title/description/content/footer는 실제 필요한 부분을 구성한다. 비어 있는 section을 형식 때문에 만들지 않는다.
- Avatar에는 fallback을 제공한다. Button loading은 실제 API를 확인하며 기본 구현에 없는 isPending/isLoading prop을 상상하지 않는다. Spinner와 disabled/status를 적절하게 조합한다.
- Toast는 base와 실제 설치된 컴포넌트에 맞춘다. 현재 원본은 Base UI toast, Radix/React Aria에는 Sonner를 안내하지만 기존 앱을 자동 migration하지 않는다.
- chat primitives가 있으면 streaming follow, anchoring, jump-to-latest는 MessageScroller 기능을 우선한다. raw scroll hook 중복을 피하고 사용자 수동 스크롤·stream 중 접근성을 확인한다. Attachment와 Marker의 역할을 유지한다.

## Base와 Radix 구분

두 base의 props와 값 형태를 섞지 않는다. 다음은 수집한 원본의 차이 요약이며 로컬 타입을 확인한다.

| 요소 | Radix 계열 | Base 계열 |
| --- | --- | --- |
| custom trigger | asChild | render |
| non-button render | asChild로 적합한 element | 필요한 경우 nativeButton={false} |
| Select placeholder | SelectValue placeholder | items의 label/value 매핑 및 null placeholder |
| Select positioning | position | alignItemWithTrigger |
| 다중/object Select | 일반 단일 string API | multiple / itemToStringValue 등 해당 API |
| ToggleGroup | type="single/multiple", 단일 string | multiple boolean, 단일도 배열 형태 |
| Slider | 단일도 배열 | 단일 number, range 배열 지원 여부 확인 |
| Accordion | type, collapsible, 단일 string | multiple, 배열 형태 |

trigger를 불필요한 div로 감싸거나 중첩 button을 만들지 않는다.

## 추가·업데이트

1. installed list/실제 경로에서 기존 component를 확인한다.
2. 선택된 registry의 docs/view를 확인한다. 기존 설정 또는 사용자의 선택이 있으면 재질문하지 않는다. 추가할 registry가 실제로 불명확하면 그 선택만 확인한다.
3. `add <component> --dry-run`으로 변경 경로를 보고 `--diff <file>`로 로컬 수정과 비교한다.
4. 필요한 파일만 merge한다. CLI가 수행하는 alias/base 변환을 활용하고 raw GitHub source를 그대로 overwrite하지 않는다.
5. 추가된 모든 관련 파일의 imports, 빠진 subcomponent, composition, iconLibrary를 확인한다. third-party block의 하드코딩된 `@/components/ui` alias를 실제 프로젝트에 맞춘다.
6. typecheck/build와 해당 interaction을 검증한다.

`--overwrite`는 로컬 변경 보존 요구와 충돌하지 않는 명시된 승인 범위에서만 사용한다. `add --all`이나 앱 초기화를 일반 polish 작업의 기본 단계로 삼지 않는다.

## Preset과 theme

preset code는 수동 decoding/URL 조합 대신 지원되는 CLI를 쓴다.

```text
npx shadcn@<PINNED_VERSION> preset resolve --json
npx shadcn@<PINNED_VERSION> preset decode <code>
npx shadcn@<PINNED_VERSION> preset url <code>
npx shadcn@<PINNED_VERSION> add button --dry-run
npx shadcn@<PINNED_VERSION> add button --diff button.tsx
```

기존 승인에서 overwrite/partial/merge/skip 의도가 정해졌으면 그대로 수행한다. 정해지지 않았고 로컬 theme/components를 교체해야 한다면 범위를 먼저 결정한다.

- overwrite: `apply <code>`가 components/font/CSS를 바꿀 수 있음을 diff로 확인한다.
- partial: 지원 버전의 `apply <code> --only theme,font` 등 선택된 부분만 적용한다. 원본에서 icon은 partial 대상이 아니다.
- merge: 원본의 `init --preset <code> --force --no-reinstall` 후 개별 component diff/merge를 검토한다. 이 명령도 config/CSS를 수정하므로 preview 없이 무해한 명령으로 취급하지 않는다.
- skip-components: 같은 no-reinstall 경로도 config/CSS는 바꿀 수 있다. “전체 변경 없음”과 구분한다.

명령은 대상 프로젝트에서 실행한다. scratch 비교라면 현재 base를 명시한다. preset code만으로 primitive base를 알 수 없다. Tailwind v3의 config와 v4의 theme CSS 방식을 혼용하지 않는다.

## Registry 작성·MCP·customization

registry 작성 요청일 때만 `registry.json`, item의 files/dependencies/registryDependencies, include·build·배포 경로를 해당 CLI schema와 [공식 registry 문서](https://ui.shadcn.com/docs/registry)로 확인한다. auth token은 코드나 공개 registry에 넣지 않는다. build와 publish는 다른 단계다.

기존 shadcn MCP가 연결되어 있으면 조회에 사용할 수 있다. 이 스킬을 읽는 행위가 MCP 설치/config 변경을 허가하지 않는다. theme 변경은 기존 global CSS/config와 tokens를 유지하며 [공식 theming 문서](https://ui.shadcn.com/docs/theming)를 필요한 경우 확인한다.
