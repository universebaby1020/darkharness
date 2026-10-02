# 공개 게이트와 디자인 팩 재현

`tools/public_guard.py`는 공식 contest layout 검사가 아니라 공개 위생 검사다. 이미 공개된 이력은 새 commit으로 지워지지 않는다. 이력 재작성은 하지 않는다.

## 비공개 설정 API v2

설정은 repo **밖의 절대 경로**로 전달한다. `--private-config` 또는 `DH_PUBLIC_PRIVATE_CONFIG`를 쓴다. 설정이 없거나 v1/잘못된 v2이면 exit 2로 차단한다. 값은 로그·ZIP·공개 소스·원장에 복사하지 않는다.

정확한 최상위 키:

- `schema_version`: `darkharness-publication-private-2`
- 비어 있지 않은 문자열 배열: `account_handles`, `band_uuids`, `project_names`, `local_filenames`, `account_emails`, `account_display_names`, `development_participant_ids`
- `band_uuids`와 `development_participant_ids`는 UUID 형식이다. 계정 이메일·표시 이름은 사용자 제공 비공개 fragment에서, 개발 participant ID는 실제 room/seat binding에서 가져온다. 별칭으로 추정하지 않는다.
- `design_pack_replacements`: `THIRD_PARTY_NOTICES.md`와 `governance/COMMON_GUIDANCE_SCOPE_KO.md`를 키로 하는 객체. 각각 비어 있지 않은 `{source, replacement}` 객체 배열이며, 배열 순서대로 **정규식이 아닌 원문 문자열 치환**을 적용한다. `project_names`와 `local_filenames`의 모든 값은 치환 source에도 있어야 한다. replacement에 비공개 목록 값이 남으면 차단한다.

모든 키는 필수다. 빈 목록·빈 치환·줄바꿈·NUL·미등록 키·유효하지 않은 UUID는 차단한다. 목록값은 내용을 출력하지 않고 literal substring으로 검출한다. fragment의 `exact_terms`는 이메일과 표시 이름 배열에 분류해 병합한다. 방 ID는 `band_uuids`, 좌석 ID는 해당 배열과 `development_participant_ids`에 넣는다. v1 원본은 보존하고 새 v2 파일을 만든다.

## 검사의 범위

- 기본 모드: 실제 index의 전체 tree, Git author/committer identity, `--message-file` 또는 `DH_PUBLIC_MESSAGE_FILE`의 예정 commit 메시지. 따라서 unstaged 수정만으로 기본 모드가 통과하지는 않는다.
- `--revision`: 정확한 commit의 전체 tree와 identity/message.
- `--base BASE --ref CANDIDATE`: BASE의 후손인지 확인하고 `BASE..CANDIDATE`의 **모든 commit**을 검사한다. merge의 다른 부모도 포함하고 첫 실패에서 중단하지 않는다. 중간 노출 commit은 tip에서 삭제했어도 차단한다.
- `--published-history REF`: 도달 가능한 과거 이력을 값 없이 기록한다. 이 모드의 exit 0은 공개 승인이나 PASS가 아니다.
- symlink/gitlink/미병합 index, credential 파일 경로는 차단한다. credential 파일 본문은 읽지 않는다.
- Windows 사용자 홈, WSL 사용자 홈과 Windows mount 사용자 경로, `.invalid` 도메인이 아닌 이메일(이름에 noreply가 포함돼도 동일)을 검출한다. `room.json`의 env-assignment 예외는 bearer나 다른 검사를 면제하지 않는다.
- Mandatory upstream attribution: 코드에 결속한 다섯 license의 **정확한 경로 + 전체 바이트 SHA-256**이 모두 일치할 때 account-handle 종류만 예외다. 변경된 license, 복사본, 다른 파일, identity/message에는 예외가 없다. 이메일·secret·UUID·다른 PII 종류도 예외가 아니다. 라이선스 파일은 삭제·변경하지 않는다.

결과는 종류·commit·가려진 파일 경로·줄번호만 담는다. `guard_sha256`과 실제 파싱한 설정 바이트의 `private_config_sha256`, 배열별 항목 수를 별도로 남긴다. 값 없는 위치 기록도 공개 허가가 아니다.

Main의 후보 검사 명령(설정 경로를 실제 외부 절대경로로 바꾼다):

```text
python -X utf8 -B tools/public_guard.py --private-config <external-absolute-config> --base 3ab37de --ref <candidate>
```

## 팩 재현

```text
python -X utf8 -B tools/prepare_design_pack.py --package <selected-supplement> --private-config <external-absolute-config>
```

대상 팩이 없는 독립 clone에서 실행한다. 원본 디자인 archive는 다시 풀지 않는다. 설정과 source literal을 먼저 검증한 뒤 기존 치환 단계를 유지한다. 라이선스 파일은 byte 그대로 복사하고 나머지 팩 텍스트만 Git publication과 같은 LF로 정규화한다. C7=1 고지를 적용하고 선별 supplement의 검증용 replica manifest를 갱신한다. replica/비공개 입력을 공개 repo나 반환 ZIP에 자동 포함하지 않는다.

WO-DH0-03R1 B의 baseline과 수정본 재현·manifest/라이선스 비교 결과는 repo 밖 운영자 evidence에 있다. baseline은 `953f357^`에서 같은 supplement로 생성한 팩/루트 고지/`.gitattributes`를 `3ab37de` Git blob과 비교했다. 수정본은 B의 unstaged bytes를 대상으로 하므로 Main의 최종 commit에 대한 정확한 Linux clone suite와 전체 후보 ref 게이트를 대신하지 않는다. Worker는 스테이징·커밋·병합·push하지 않는다.
