# 좌석별 연결·모델·effort·시간제한

기존 진단의 모델·인증·실행 환경은 변경하지 않는다. 이 기능은 사용자가 **후속 run**에서 선택한 설정을 적용한다. 역할과 연결, 모델은 별개이며 역할별 모델 표나 공급자 fallback은 없다. GUI/설치기는 이 범위에 없다.
선택적 result_repo·timeout 준비·진단·guard와 판본별 검증 수준은 [WO04 공개 factory 계약](wo04-factory-contract.md)을 따른다.

## 설정과 우선순위

기존 `run.example.json`의 flat `model`, `effort`, `codex_command`, `runtime_env`는 그대로 `legacy` Codex 연결로 해석한다. `turn_timeout_s` 생략 기본값은 **3600초**다.

새 JSON 필드:

- `result_repo`: 선택적 명시 canonical 절대경로. 실제 workspace 하위의 독립 Git repo를 config와 기존 Grant scope에 동일하게 등록한다. workspace native cwd/권한 상한은 그대로이며, 생략하면 기존처럼 같은 root를 쓴다. local_git_write 경로 capability는 결과 repo 상대경로다.
- `auto_recover_settled_timeouts`: default-off. true 명시와 같은 run/workspace 및 허용 room/seat의 Grant capability가 함께 필요하다. 순수 `prepare_settled_timeout_recovery(scope, *, run_id, workspace, rooms, seats)` 반환 scope는 run 시작 전 기존 `grant.record`로 등록한다. helper는 lifecycle/admission 보장이 아니며 기존 SDK terminal proof와 UNKNOWN fence를 유지한다.

- `connections.<name>`: 필수 `runtime`, 선택적 `command`, `runtime_env`, `model`, `effort`, `turn_timeout_s`. runtime은 등록된 `codex` 또는 `claude_code`. 모델이 보낸 import/plugin/callable/권한 정책을 설정으로 받을 수 없다.
- `default_connection`: 좌석에서 참조를 생략했을 때 사용할 이름. profiles를 선언한 경우 기본 연결을 추정하지 않는다.
- `seats[].connection`: 이름 참조. 좌석은 독립적으로 `model`, `effort`, `turn_timeout_s`를 override한다.
- 모델/effort: **seat > connection > 같은 runtime의 run 기본값**. 다른 runtime의 run 모델/effort를 상속하면 `RUNTIME_DEFAULT_MISMATCH:<seat>:<field>`. Claude 연결에 Codex 모델을 몰래 다른 모델로 바꾸지 않는다.
- timeout: **seat > connection > run > 3600**. 양의 유한 SDK seconds int/float만 허용한다. bool, NaN, infinity, 0, 음수, 문자열은 거절한다. 별도 정책 상한은 없다.
- command: connection > 기존 Codex run command. Claude는 단일 native Linux CLI 실행 경로를 지정한다. Codex argv는 CLI 경로와 `app-server`/선택적 `--listen stdio://` 형식뿐 아니라 기존 승인된 `[node, <absolute>/node_modules/@openai/codex/bin/codex.js, app-server, --listen, stdio://]`도 보존한다. Node 형식은 preflight에서 entrypoint 존재와 공식 `@openai/codex` package name/version `0.159.3`/bin identity를 확인한다. 임의 Node script, shell wrapper, `-c` 설정, 권한 우회 flag를 profile 권한으로 인정하지 않는다.
- env: connection의 전체 env > 같은 runtime의 run env > 빈 값. 병합하지 않는다. Codex는 `PATH`, `CODEX_HOME`; Claude는 `PATH`, `CLAUDE_CONFIG_DIR`만 허용한다. auth home은 workspace 밖 절대경로 참조이며 내용은 읽거나 복사하지 않는다. WSL Codex auth home은 Windows mount를 사용하지 않는다. Claude Windows CLI/interop는 보호된 Linux process-group backend로 광고하지 않고 거절한다.

unknown 필드/연결/runtime, 빠진 command/model/effort는 위치를 포함한 오류로 반환한다. SDK별 지원 effort와 설치 버전·command 존재 여부는 startup의 모든 좌석 local preflight에서 검사한다. Codex의 모델/effort 실제 지원 여부는 native SDK의 model/list 및 서버 검증에 따른다. 문자열 이름 자체는 권한이 아니다. Native 모델이 요청 모델과 다르거나 Claude terminal이 없어지면 UNKNOWN으로 남기며 성공·fallback으로 처리하지 않는다.

### 후속 mixed-run 예시 (설명용, live config가 아님)

아래는 기존 run 필수 identity/workspace/credential-reference와 세 seat identity를 유지하면서 추가할 부분이다. 사용자는 role에 상관없이 연결·값을 다시 선택할 수 있다. Claude model placeholder는 실제 선택한 **정확한 ID**로 교체한다. 예시가 모델 선택이나 실행 승인을 대신하지 않는다.

```json
{
  "model": "gpt-6-astra",
  "effort": "high",
  "turn_timeout_s": 3600,
  "default_connection": "codex-primary",
  "connections": {
    "codex-primary": {
      "runtime": "codex",
      "command": ["/opt/native/codex", "app-server", "--listen", "stdio://"],
      "runtime_env": {"CODEX_HOME": "/srv/private/codex-primary"}
    },
    "claude-build": {
      "runtime": "claude_code",
      "command": ["/opt/native/claude"],
      "runtime_env": {"CLAUDE_CONFIG_DIR": "/srv/private/claude-build"},
      "model": "replace-with-selected-claude-model-id",
      "effort": "high",
      "turn_timeout_s": 3600
    },
    "codex-review": {
      "runtime": "codex",
      "command": ["/opt/native/codex", "app-server"],
      "runtime_env": {"CODEX_HOME": "/srv/private/codex-review"},
      "model": "gpt-6-astra",
      "effort": "high"
    }
  }
}
```

기존 seats에 각각 `connection: codex-primary`, `connection: claude-build`, `connection: codex-review`를 넣을 수 있다. 어느 seat든 별도 모델/effort/timeout을 지정할 수 있다. 지금의 diagnostic 설정 파일은 수정하지 않는다.

## CLI와 고정된 실행 의미

```text
python -B -m darkharness.integration validate --config <external-json>
python -B -m darkharness.integration prepare --config <external-json> --official-root <trusted-official-checkout>
```

`validate`는 순수 설정/경로 구조 검사다. 인증 파일 내용, 로그인, provider inference, native CLI probe를 수행하지 않는다. `effective_settings`에 seat별 connection/runtime/model/effort/timeout, 각 값의 출처, SHA-256 fingerprint를 반환한다. command/env/auth-home/credential path 원문은 출력하지 않는다. `LOCAL_PREFLIGHT_REQUIRED`, `execution_qualification: NOT_RUN`은 실행 검증 성공을 뜻하지 않는다.

`prepare`는 effective binding에 맞는 정확한 Model/effort/Harness mandate를 생성한다. optional backend 실패는 mandate 쓰기 전에 발생한다. 기존 Codex prepare는 의존성/인증 없이 가능하다. startup은 caller JSON을 deepcopy하고 모든 backend를 preflight한 뒤 credentials/Agent/room을 다룬다.

`SeatSettings`는 frozen tuple 기반이다. fingerprint는 connection 이름, runtime, canonical workspace, model, effort, timeout, command, env 및 명시된 result_repo를 포함하고 **설정 출처는 제외**한다. result_repo 생략 시 기존 fingerprint 형식을 유지한다. 생략한 기본값과 같은 explicit 값은 실행 의미가 같다. `RuntimeBinding`도 출처만 다른 경우 동등하다. 같은 run의 seat identity/role/room/effective fingerprint는 Store의 `c_run_settings`에 고정하며 재시작 때 변경을 거절한다. 다른 값을 선택하려면 새 run identity를 사용한다. 모델 output/룸 command로 설정을 바꾸거나 승인을 추론하지 않는다.

Thread/session 선택은 run/room/seat **및 binding fingerprint**를 확인한다. 다른 runtime/auth connection/모델/timeout의 session이나 미확인 legacy session은 resume하지 않는다. 자기 durable 원래 작업·결과·실제 peer answer는 context/evidence로 보존하고, 과거 도구 효과를 replay하지 않는다. Codex의 기존 native history/toolset cutover를 유지한다. Claude는 schema/mandate가 바뀌면 자기 durable history artifact를 연결하고 새 native session을 쓴다. 구현 source hash는 별도 provenance이며 source만 바뀌었다고 compatible session을 버리지 않는다.

## 실제 optional Claude Code backend

등록 factory는 `DurableClaudeAdapter`/`ClaudeRuntime`를 구성한다. `DurableCodexAdapter`로 대체하지 않는다. 실제 Band SDK4 `ClaudeSDKAdapterConfig`, history/metadata 계약과 실제 `claude_agent_sdk` client, MCP server, PreToolUse/can_use_tool API를 쓴다. raw Anthropic API는 사용하지 않는다.

별도 Linux 환경의 검사된 의존성 pin:

```text
band-sdk==4.0.0
claude-agent-sdk==0.2.163
mcp==1.30.0
```

active shared venv/CLI에 설치하지 않는다. 사용자가 선택한 연결의 공식 native 로그인은 외부에서 준비한다. validation/preflight는 로그인하지 않는다. 선택 dependency가 없으면 `CLAUDE_BACKEND_DEPENDENCY_MISSING`; 버전이 다르면 `CLAUDE_DEPENDENCY_VERSION_UNSUPPORTED`; native 실행 파일이 없으면 `CLAUDE_COMMAND_UNAVAILABLE`. Codex만 선택했으면 Claude/MCP import가 없어도 validate/preflight/factory/Runtime.start가 동작한다.

보호 경로:

- 공통 `DurableSeatMixin`, `GuardedTools`, `DurableRuntime`로 mailbox/Core/typed Git/verification/질문/continuation/status/error를 라우팅한다. upstream의 unguarded Band MCP, room `/approve`, provider fallback, raw session-manager resume fallback은 사용하지 않는다.
- native tools는 Read/Glob/Grep/Write/Edit/Bash만 노출한다. PreToolUse에서 정확한 workspace/attempt/현재 Grant를 검사하고, 파일 경로와 exact command를 Controller ApprovalRouter로 판단한다. 알 수 없는 tool/privilege/script/shell chain은 거절한다. MCP도 captured exact room/seat/attempt/Grant facade를 통과한다. 선택 connection은 plugin/script 실행 허가가 아니다.
- native `default` 권한 모드, 빈 setting_sources/plugins, strict MCP 설정을 사용한다. 별도 owned Linux process group과 PID/boot/start identity로 취소한다. 보호 수준은 **native-controlled**이며 same-UID, 외부 Docker/network cleanup에 대한 OS 격리를 주장하지 않는다.
- 최소 native 환경 참조만 전달하고 ambient API key/token/provider URL/Bedrock/Vertex 설정은 상속하지 않는다. API billing으로 자동 전환하지 않는다. config/auth home은 외부 참조만 사용한다.
- `dh_local_git_commit`, `dh_review_snapshot`, `dh_verify`, `dh_verification_read`는 기존 trusted broker를 그대로 사용한다. checker 대기는 model turn timeout과 분리한다. UNKNOWN/취소/STOP/revoke는 자동 continuation/replay 근거가 아니다.
- Claude 메시지는 `CLAUDE_NATIVE_MESSAGE`, permission/process/session 이벤트로 실제 provenance를 남긴다. Codex RPC/terminal 이벤트를 만들지 않는다. Codex-only timeout proof를 Claude 성공 정산에 사용하지 않는다.

Claude readiness의 `ready: true, level: LOCAL_COMPONENT`는 설치/type/executable 경로 수준이다. `authentication: NOT_PROBED`, `inference: NOT_RUN`, `execution_qualification: NOT_RUN`을 함께 반환한다. 실제 CLI 모델/인증/room interoperability/추론 검증은 별도 사용자 선택 후의 작업이다. fake-native component PASS를 실계정 실행 자격으로 바꾸지 않는다.

집중 시험 및 실제 stdout/stderr·source hash는 `evidence/seat-settings/`에 기록한다. 전체 exact-export 검사는 Main이 수행한다.
