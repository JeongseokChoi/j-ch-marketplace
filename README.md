# j-ch-marketplace

Claude Code 플러그인 모음.

## workflowy — Workflowy Workstream Log

Workflowy 노드 하나를 작업 흐름(workstream)의 기록으로 삼아, Claude Code 세션들이 그 아래에 작업 과정을
이어서 정리해 기록한다.
작업을 **지금 지켜보고**, **남겨 두고**, **나중에 파악**하기 위한 기록이다.

### 설치

```bash
claude plugin marketplace add JeongseokChoi/j-ch-marketplace
claude plugin install workflowy@j-ch-marketplace --config api_key=<WORKFLOWY_API_KEY>
```

- API Key: https://workflowy.com/api-key 에서 발급
- Python 3 이 `python3` 으로 실행되어야 한다 (훅과 MCP 서버 모두 표준 라이브러리만 쓴다).

### 사용

작업 흐름마다 Workflowy 노드를 하나 만들고, 세션마다 그 노드로 기록을 시작한다.
시작하지 않은 세션은 아무것도 기록하지 않는다.

```
/workflowy:workstream daa0961ddeee                        # 이 노드 아래에 기록 시작 (short id)
/workflowy:workstream https://workflowy.com/#/daa0961ddeee  # URL 도 된다
/workflowy:workstream daa0961ddeee 로그인 오류 고쳐줘       # id 뒤의 글은 첫 요청으로 전달된다
/workflowy:workstream                                     # 현재 상태: root, 쓴 노드, 지금 작업 중인 노드
/workflowy:workstream sync                                # Workflowy 에서 root 아래 전체를 다시 읽어 이어받기
/workflowy:workstream stop                                # 기록 중단 (쓴 노드는 그대로)
/workflowy:workstream doctor                              # 설정·연결·트리 읽기·최근 오류 점검
```

지정한 노드 자체는 건드리지 않는다. 그 아래를 Claude 가 작업에 맞게 구성한다.
지정한 노드 아래에 이미 있는 기록은 **이어받는다**. Claude 는 지금까지의 트리
(길면 요청 목록, 열린 todo, 보류된 todo, 마지막 요청. 요청 옆에 날짜)를 받아 흐름을 파악하고,
이전 요청 아래에 덧붙이거나 남은 todo 를 이어서 할 수 있다.

- **시작할 때**는 요청 목록만 Workflowy 에서 읽고(API 1번), 요청의 하위는 이 PC 에 남은 세션 상태를 쓴다.
- **`sync`** 는 root 아래 전체를 Workflowy 에서 읽는다. 다른 PC 에서 쓴 기록과 Workflowy 에서 직접 적거나
  체크하거나 지운 내용이 반영된다. 세션 중 언제든 부를 수 있고, 이 세션이 만든 노드는 그대로 둔다.
  노드마다 API 를 한 번씩 부르므로 시간이 걸린다 (병렬 8개. 노드 350개쯤이면 20초 남짓).
  그래서 **백그라운드에서 읽는다**: 훅은 프로세스를 띄우고 곧바로 끝나고(훅 timeout 과 무관, 시간 제한 없음),
  다 읽으면 결과가 사용자의 다음 메시지나 Claude 의 다음 workflowy 도구 결과 뒤에 한 번 전해진다.
  도는 동안은 `/workflowy:workstream` 에 진행 상황(읽은 노드 수, 경과 시간)이 보인다. 노드 수에는 ▹ 도구 실행 노드를
  세지 않으므로 끝난 뒤 알리는 수와 같은 기준이다. 결과를 전하기 전에 `sync` 를 다시 부르면 앞 결과를 먼저 전하고 새로 읽는다.
  호출이 실패한 부분은 이 PC 의 세션 상태로 채우고 `[하위 일부: 이 PC 기록]` 으로 표시하며, 그 노드 제목을 알린다.
- 끝나지 않은 todo 가 있으면 Claude 는 혼자 닫지 않고 목록을 보여 주며 어떻게 할지 묻는다.
  그래서 Workflowy 에 todo 를 적어 두고 `sync` 하면 할 일로 넘길 수 있다.
- API 를 읽지 못하면 시작은 이 PC 의 세션 상태로만 이어받고, `sync` 는 상태를 그대로 둔다. 어느 쪽이든 그렇다고 알린다.

### 기록되는 구조

Claude 가 내용에 맞는 노드 타입을 골라 쓴다. 따로 정하지 않으면 대략 이렇게 된다.

```
📁 인증 개편                                      ← 지정한 노드 = 작업 흐름 (건드리지 않음)
├── **인증 미들웨어 3단계 분리**                    요청 (request: true. 굵게·note 의 날짜·시각은 자동)
│   ├── ❝ 요청 원문 ❞                               quote-block
│   ├── 계획: verify/extract/inject 로 나눈다       p
│   ├── ☑ Phase 1 · 구조 파악                       todo (완료)
│   │   ├── ▹ 미들웨어 파일 탐색                     도구 실행 (훅이 자동으로 붙임)
│   │   └── 발견: 세 역할이 한 함수에 섞여 있음       bullets
│   ├── ☐ Phase 2 · 분리 구현                       todo (진행 중)
│   │   ├── ▹ 편집 · auth.py
│   │   └── ┌ pytest -k auth ┐                      code
│   └── 결과: 테스트 12건 통과                       p
└── **다음 요청 …**                                 다음 세션의 요청도 여기에 이어진다
```

| 타입 | 쓰임 |
|---|---|
| `bullets` | 발견, 결정, 막힌 점. 요청 안의 큰 주제도 bullets 로 두고 하위를 들여 쓴다 |
| `todo` | Phase, 단계, 하위 작업. 끝나거나 멈출 때마다 닫는다 (아래) |
| `p` | 계획, 결과 같은 단락 |
| `quote-block` | 요청 원문, 인용할 출력 |
| `code` | 핵심 명령, 오류 메시지, 짧은 코드 |

제목 서식(h1·h2·h3)은 쓰지 않는다. 구획은 bullets 와 들여쓰기로 나눈다.

이름은 **평문**이다. 서식은 요청 제목의 굵게만 서버가 붙인다. Workflowy API 는 이름을 마크다운과 HTML 로 해석하므로,
서버가 서식이 되는 글자를 비슷한 글자로 바꿔 보낸다.

| 글자 | 바꾼 글자 | 이유 |
|---|---|---|
| `<` `>` (이름·note·코드 블록) | `‹` `›` | 날것이든 엔티티(`&lt;`)든 백슬래시를 붙이든 태그가 되고, 모르는 태그가 있으면 이름이 통째로 사라진다 |
| `*` `` ` `` | `∗` `ˋ` | 굵게·기울임·코드가 되며 글자가 사라진다. 백슬래시로 막으면 백슬래시가 화면에 남는다 |
| `~~` `](` | `∼∼` `］(` | 취소선·링크 |
| 줄머리 `# ` `- [ ] ` | `＃ ` `‐ [ ] ` | 제목·todo 로 바뀐다 |

**root 바로 아래에는 요청만 들어간다.** Claude 는 새 요청을 `create(parent=root, request: true)` 로 만들고,
서버가 제목을 굵게 하고 note 첫 줄에 날짜·시각을 붙인다. 요청이라고 밝히지 않은 노드를 root 바로 아래에
쓰려 하면 훅(`guard`)이 거부하고 지금 요청의 id 를 알려 준다 (하위에 쓰려던 노드가 실수로 root 에 들어가는 것을 막는다).

**지금 하는 일**은 열려 있는 todo 로 보인다. `▹` 항목은 Claude 가 부른 도구의 description
(Bash 등)과 편집한 파일 이름으로, 훅이 지금 작업 중인 노드 아래에 자동으로 붙인다.
지금 작업 중인 노드는 트리 순서로 닫히지 않은 첫 todo(그 안에 열린 todo 가 있으면 가장 안쪽, 보류된 todo 는 건너뜀),
열린 todo 가 없으면 이 세션에서 마지막으로 만든 요청이다. 이어받은 노드에는 붙이지 않는다. description 이 없는 읽기 도구(Read·Grep 등)와
서브에이전트가 부른 도구는 붙이지 않는다. 서브에이전트는 기록하지 않고, 메인 세션이 결과를 정리한다.

### todo 닫기

Claude 는 todo 를 `close` 도구로 닫으면서 어떻게 닫는지 고른다. 하지 않은 일이 '완료' 로 남지 않게 하려는 것이다.

| outcome | 뜻 | Workflowy 에 남는 것 |
|---|---|---|
| `done` | 끝냈다 | 체크 (아래에 쓴 결과가 없으면 `결과: …` 한 줄이 필수) |
| `cancel` | 하지 않기로 했다 | `✕ 취소: 이유` + 체크 |
| `replace` | 계획이 바뀌어 다른 방법으로 대신한다 | `↪ 변경: 이유` + 체크 |
| `hold` | 나중에 이어서 한다 | `⏸ 보류: 이유`, 체크하지 않음 |

- 훅(`guard`)이 결과 없는 done, 이유 없는 cancel·replace·hold, 닫지 않은 하위 todo 가 남는 닫기를 거부한다.
- 계획이 바뀌면 남은 todo 를 한꺼번에 `replace` 로 닫고 새 계획과 todo 를 아래에 쓴다.
- 보류된 todo 에는 도구 실행이 붙지 않는다. 이어서 할 때는 그 아래에 재개 todo 를 만들고, 거기서부터 도구 실행이 붙는다.

```
├── ✓ Phase 1 · 파일 포맷 분석
├── ☐ Phase 2 · 데이터 추출
│   ├── ⏸ 보류: 원본이 암호화됨 — 설치 후 재진행
│   └── ☐ 재개 · .prx 에서 추출                    다음 세션이 만든 todo (지금 작업 중)
│       └── ▹ .prx 컨테이너 해제
└── ☐ Phase 3 · 문서 작성
    └── ⏸ 보류: 원본이 암호화됨 — 설치 후 재진행
```

### 동작 방식

| 구성 | 역할 |
|---|---|
| MCP 서버 `workflowy` (`scripts/mcp.py`) | Claude 가 쓰는 도구 `create`(노드 추가. `request: true` 면 요청 노드), `close`(todo 닫기: 완료·취소·방향 전환·보류). 수정·삭제 도구는 없다 |
| PreToolUse 훅 `guard` | 기록 중인 세션의 메인 Claude 가, root 또는 이 세션에서 만들었거나 이어받은 노드 아래에만 쓰도록 검사. root 바로 아래는 요청만. todo 를 닫을 때 결과·이유·하위 todo 도 검사. 범위 안이면 권한 확인 없이 허용 |
| PostToolUse 훅 `track` | 만든 노드와 닫은 todo(보류 포함)를 세션 상태에 기록. 끝난 `sync` 결과가 있으면 도구 결과 뒤에 덧붙여 전함 |
| PreToolUse 훅 `step` | 도구 실행을 지금 작업 중인 노드 아래에 `▹` 로 추가 |
| UserPromptSubmit 훅 `prompt` | `/workflowy:workstream` 인자 처리와 이어받기(시작 때 요청 목록 읽기, `sync` 때 백그라운드 프로세스 띄우기). 기록 중이면 요청마다 기록 지침을 한 줄로 상기하고, 끝난 `sync` 결과를 전함 |
| 백그라운드 `wf.py sync-run` | `sync` 가 띄우는 분리된 프로세스. root 아래 전체를 끝까지 읽고, 잠금을 잠깐 잡아 세션 상태에 합친 뒤 결과를 남김 |
| SessionStart 훅 | 대화 압축·재개 뒤 기록 중인 root 와 지금까지 만든 노드(id 포함)를 Claude 에게 다시 알림 |

- 세션 상태는 `${CLAUDE_PLUGIN_DATA}/state/<session id>.json` 에 있다. `stop` 하거나 다른 노드로 바꾸면
  `<session id>.<시각>.json` 으로 남겨 둔다. 시작할 때 요청의 하위, `sync` 때 Workflowy 에 없는 정보(todo 의 `steps`)와
  다 읽지 못한 부분을 이 파일들에서 채운다.
- `sync` 의 진행 상황과 결과는 `${CLAUDE_PLUGIN_DATA}/sync/<session id>.json` 에 있다. 세션당 하나만 돌고,
  프로세스가 결과 없이 사라지면 다음 확인 때 "중단됨" 으로 알린다. 도중에 기록을 멈추거나 root 를 바꾸면 합치지 않는다.
- `/clear` 는 새 세션이 되므로 기록이 끊긴다. 같은 노드로 다시 시작하면 이어받는다.

### 주의

- 기록 내용의 토큰류는 정규식으로 마스킹하지만 완전하지 않다.
  사내 토큰 형식은 `scripts/wfapi.py` 의 `SECRET` 패턴에 추가할 것.
- 기록 실패는 세션을 막지 않는다. 훅 오류는 `${CLAUDE_PLUGIN_DATA}/error.log` 에 쌓이고,
  다음에 기록을 시작할 때 새 오류가 있으면 Claude 가 알린다. MCP 도구의 실패는 Claude 가 그 자리에서 본다.
- Workflowy API 는 layoutMode 값을 검증하지 않는다 (모르는 값은 bullet 으로 그려진다).
  MCP 서버가 위 타입만 받는다 (h1·h2·h3 도 받지 않는다). 코드 블록은 layoutMode 대신 ``` 로 감싸 보내야 여러 줄이 한 블록에 들어간다.

### 2.x 에서 옮겨 오기

3.0 은 스킬 이름이 `/workflowy:session-log` 에서 `/workflowy:workstream` 으로 바뀌었다.
지정한 노드가 세션 보관함이 아니라 작업 흐름 하나가 되어, 세션마다 구획을 만들지 않고
요청을 노드 바로 아래에 이어 쓴다. 2.x 로 쓴 노드도 같은 노드로 시작하면 이어받는다
(그 PC 에 세션 상태가 남아 있고 `stop` 하지 않은 세션만).
3.1 부터 요청은 h2 대신 굵은 bullets 로 쓰고, 제목 서식(h1·h2·h3)은 쓰지 않는다.
3.2 부터 todo 는 `complete` 대신 `close` 로 닫고, 취소·방향 전환·보류를 구별해 남긴다.
그 전에 완료한 todo 는 이어받을 때 완료(✓)로 보인다.
3.3 부터 요청은 `request: true` 로 만들고 root 바로 아래에는 요청만 들어간다. 요청 note 에는 날짜·시각만 자동으로 붙는다
(작업 디렉터리 이름은 더 쓰지 않는다). 그 전의 h2·굵은 bullets 요청은 이어받을 때 그대로 요청으로 본다.
3.4 부터 이어받기는 Workflowy 를 읽는다. 시작할 때는 요청 목록만, `sync` 로는 root 아래 전체를 백그라운드에서 읽는다.
root 바로 아래 노드는 모두 요청으로 보고, 닫은 방식(취소·변경)과 보류는 `close` 가 남긴 이유 노드로 알아본다.
끝나지 않은 todo 는 Claude 가 사용자에게 묻는다. 이름은 평문으로 보내고(요청 굵게만 유지) 꺾쇠는 `‹ ›` 로 바꾼다.
prompt 훅 timeout 은 30초가 되었다.

### 1.x 에서 옮겨 오기

2.0 은 기록 방식이 바뀌었다. 세션이 자동으로 기록되지 않고, 설정의 `root_id` 대신
스킬 인자로 노드를 받는다. 턴·진행 단계 정리(`claude -p` 요약)와 `note`/`link`/`close` 인자는 없어졌다.

### 업데이트

```bash
claude plugin update workflowy
```

유지보수자는 `plugins/workflowy/.claude-plugin/plugin.json` 의 `version` 을 올리고
push 해야 사용자에게 갱신이 전달된다.

### 제거

```bash
claude plugin uninstall workflowy
```
