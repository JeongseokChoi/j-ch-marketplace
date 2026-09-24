# j-ch-marketplace

Claude Code 플러그인 모음.

## workflowy — Workflowy Workstream Log

Workflowy 노드 하나를 작업 흐름(workstream)의 기록으로 삼아, Claude Code 세션들이 그 아래에 작업 과정을
이어서 정리해 기록한다.
작업을 **지금 지켜보고**, **남겨 두고**, **나중에 파악**하기 위한 기록이다.
노드 하나와 그 하위 전체를 파일로 받아 Claude 에게 읽히는 `/workflowy:snapshot` 도 있다 (아래 **노드 스냅숏**).

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
/workflowy:workstream sync                                # Workflowy 에서 root 아래 전체를 다시 받아 cache 를 새로 채우기
/workflowy:workstream clear-cache                         # cache 비우기 (옛 cache 에서 이어 오던 값을 끊는다)
/workflowy:workstream stop                                # 기록 중단 (쓴 노드는 그대로)
/workflowy:workstream doctor                              # 설정·연결·트리 읽기·최근 오류 점검
```

지정한 노드 자체는 건드리지 않는다. 그 아래를 Claude 가 작업에 맞게 구성한다.
지정한 노드 아래에 이미 있는 기록은 **이어받는다**. Claude 는 지금까지의 트리
(길면 요청 목록, 열린 todo, 보류된 todo, 마지막 요청. 요청 옆에 날짜)를 받아 흐름을 파악하고,
이전 요청 아래에 덧붙이거나 남은 todo 를 이어서 할 수 있다.
트리에는 제목(60자까지)만 보이고, 요청의 내용(제목 전체·note·코드 원문)이 필요하면 Claude 가 `read` 도구로 그 요청만 읽는다
(아래 **cache 와 state**).

- **시작할 때**는 요청 목록만 Workflowy 에서 읽고(API 1번), 요청의 하위는 이 PC 의 cache 를 쓴다 (아래 **cache 와 state**).
  안내에 cache 를 언제 받았는지(마지막 `sync`, 비운 시각)가 붙는다.
- **`sync`** 는 root 아래 전체를 Workflowy 에서 읽어 **cache 를 통째로 바꾼다**. 다른 PC 에서 쓴 기록과 Workflowy 에서 직접 적거나
  체크하거나 지운 내용이 반영되고, 이 세션이 이어받은 부분도 바뀐다. 세션 중 언제든 부를 수 있고, 이 세션이 만든 노드는 그대로 둔다.
  Workflowy 에 없는 값(todo 의 `steps`)과 못 읽은 하위는 옛 cache 에서 이어 온다.
  단락·인용·코드 노드와 `close` 가 쓴 이유 노드의 하위도 읽는다. ▹ 도구 실행 노드는 읽지도 담지도 않는다.
  노드마다 API 를 한 번씩 부르므로 시간이 걸린다 (병렬 8개. 노드 350개쯤이면 20초 남짓).
  그래서 **백그라운드에서 읽는다**: 훅은 프로세스를 띄우고 곧바로 끝나고(훅 timeout 과 무관, 시간 제한 없음),
  다 읽으면 결과가 사용자의 다음 메시지나 Claude 의 다음 workflowy 도구 결과 뒤에 한 번 전해진다.
  도는 동안은 `/workflowy:workstream` 에 진행 상황(읽은 노드 수, 호출 수, 한도 대기 횟수, 경과 시간)이 보인다.
  노드 수에는 ▹ 도구 실행 노드를 세지 않으므로 끝난 뒤 알리는 수와 같은 기준이다.
  결과를 전하기 전에 `sync` 를 다시 부르면 앞 결과를 먼저 전하고 새로 읽는다.
  Workflowy 의 요청 한도(HTTP 429)에 걸리면 모든 호출을 멈추고 기다렸다가 다시 읽는다. `Retry-After` 가 있으면 그 값
  (최대 120초), 없으면 성공 없이 429 가 이어질 때마다 10·20·30·30·60·60초로 늘린다. 다 기다리고도 막혀 있으면 포기하고
  남은 부분은 부르지 않는다. 짧은 간격으로 `sync` 를 여러 번 부르면 이 한도에 걸린다.
  그래도 읽지 못한 부분은 옛 cache 로 채우고 `[하위 일부: cache]` 로 표시하며, 그 노드 제목과
  실패 이유(예: `HTTP 429 9번`)를 알린다. 훅과 MCP 도구의 호출은 응답이 늦어지지 않도록 짧게(1.5초·3초) 다시 시도하고 만다.
- **`clear-cache`** 는 이 root 의 cache 를 비우고, 이 세션이 이어받은 노드도 요청과 이 세션이 쓴 노드의 조상만 남긴다.
  root 등록과 이 세션이 쓴 노드, Workflowy 는 건드리지 않는다. `sync` 가 옛 cache 에서 이어 오는 값이 잘못됐을 때 그 연결을
  끊는 유일한 길이다. 옛 cache 없이 새로 받으려면 `clear-cache` 뒤에 `sync`. `sync` 가 도는 중에는 거부한다.
  비운 뒤 시작하는 세션에는 요청 제목과 비운 뒤에 쓴 노드만 보인다.
- 끝나지 않은 todo 가 있으면 Claude 는 혼자 닫지 않고 목록을 보여 주며 어떻게 할지 묻는다.
  그래서 Workflowy 에 todo 를 적어 두고 `sync` 하면 할 일로 넘길 수 있다.
- API 를 읽지 못하면 시작은 cache 로만 이어받고, `sync` 는 cache 와 state 를 그대로 둔다. 어느 쪽이든 그렇다고 알린다.

### cache 와 state

플러그인이 이 PC 에 두는 기록은 둘이고, 쓰임이 다르다.

| | cache | state |
|---|---|---|
| 무엇 | root 하나의 트리를 이 PC 가 아는 사본 | 세션 하나의 기록 상태 |
| 파일 | `${CLAUDE_PLUGIN_DATA}/cache/<root>.json` | `${CLAUDE_PLUGIN_DATA}/state/<session id>.json` |
| 담는 것 | `sync` 로 받은 트리(본문·제목 앞 60자 이름표·타입·닫힘·보류·`steps`)와 받은 시각 | 기록 중인 root, 이 세션의 노드(만든 것은 본문까지, 이어받은 것은 본문 없이), 지금 작업 중인 노드 |
| 누가 쓰나 | `sync`(통째로 바꿈), `clear-cache`(비움) | 그 세션의 훅 (`track` 이 만든 노드와 닫은 todo 를 기록) |
| 지우면 | Workflowy 에서 다시 받으면 된다 (원본은 Workflowy) | 기록이 끊긴다 (쓸 수 있는 parent 를 모른다) |

```
Workflowy ──sync──▶ cache ──시작──▶ state 의 이어받은 노드
세션이 만든 노드·닫은 todo ──track──▶ state
```

- 시작할 때 이어받는 하위는 cache 에, **cache 를 받은(비운) 뒤** 이 PC 의 다른 세션들이 만든 노드와 닫은 todo 를 더한 것이다.
  그래서 `sync` 를 기다리지 않아도 이 PC 에서 이어진 작업이 보인다. state 의 이어받은 노드는 다른 세션이 읽지 않는다 —
  cache 보다 낡은 사본이기 때문이다.
- create·close 는 Workflowy 를 곧바로 부르므로 세션이 쓴 것은 이미 Workflowy 에 있다. 그래서 다음 `sync` 의 cache 에 저절로 들어간다.
- 노드의 **본문**(제목 전체·note, 코드 블록은 원문)은 cache 와, 세션이 만든 노드의 state 에만 둔다.
  이어받은 노드를 state 로 옮길 때는 본문을 뺀다 — state 는 세션마다 쌓이고 훅이 도구마다 읽고 쓰기 때문이다.
  세션이 만든 노드의 본문은 `create` 가 실제로 보낸 글(비밀값 가림·비슷한 글자 바꿈 뒤)이라 다음 `sync` 로 받는 글과 같다.
- 이어받기 안내에는 60자 이름표만 나오고, Claude 는 필요한 노드의 본문을 `read` 도구로 읽는다. `read` 는 Workflowy 를 부르지 않고
  cache 에, 받은 뒤 이 PC 의 세션들이 쓴 것을 더해 읽는다. 읽을 수 있는 범위는 `create` 의 parent 와 같다 (root 와 이어받았거나
  이 세션이 만든 노드). 3만 자를 넘으면 멈추고 이어 읽을 노드의 id 를 알려 준다. 3.6 전에 받은 cache 의 노드는 본문이 없어
  `[본문 없음]` 으로 보인다 (`sync` 하면 채워진다).

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

### 노드 스냅숏

```
/workflowy:snapshot daa0961ddeee                          # 이 노드와 그 하위 전체를 파일로 받는다
/workflowy:snapshot daa0961ddeee 요약해줘                  # id 뒤의 글은 요청으로 전달된다
```

- 노드 하나와 그 하위 전체(자식·손자 …)를 Workflowy 서버에서 읽어 이 세션의 scratchpad 에 `workflowy-<short id>.md` 로
  저장한다. context 에는 파일 경로와 요약(노드 수·줄 수·크기)만 남고, Claude 는 필요한 부분만 Grep·Read 로 읽는다.
  같은 노드를 다시 받으면 덮어쓴다. scratchpad 가 없는 세션은 OS 임시 폴더의 `workflowy-snapshot/<session id>/` 에 둔다.
- **읽기만 한다.** Workflowy 에 쓰지 않고 workstream 의 cache·state 도 바꾸지 않는다 (기록에 쓸 트리를 새로 받는 것은 `sync`).
  기록 중이 아니어도 되고 어떤 노드든 된다. 읽는 것은 그 노드와 하위뿐이다.
- **사용자만 부른다.** Claude 는 이 스킬을 부를 수 없다 (`disable-model-invocation`). 읽기는 사용자가 친 명령을 받은 훅만
  시작하고, Claude 의 Bash 에는 API key 가 없다.
- **백그라운드에서 읽는다.** 훅은 프로세스를 띄우고 곧바로 끝난다 (훅 timeout 과 무관). 읽기는 `sync` 와 같다: 병렬 8개, 끝까지,
  요청 한도(429)에는 모두 멈춰 기다린다. Claude 는 훅이 알려 준 `wait` 명령을 Bash 백그라운드로 실행해 두고, 끝나면 결과를 받아
  요청을 이어서 한다. `wait` 를 부르지 않았으면 결과는 사용자의 다음 메시지 뒤에 한 번 전해진다. 끝나기 전에는 파일이 없다.
- 파일은 **Workflowy 가 준 값만** 담는다. 읽는 트리에 무엇이 있을지 모르므로 workstream 의 규칙(▹ 도구 실행 빼기, 요청·닫은
  방식 알아보기 등)은 적용하지 않는다. ▹ 노드도 보통 노드로 담고 그 하위도 읽는다 (그래서 기록 root 는 `sync` 보다 호출이 많다).
  비밀값도 가리지 않는다.

  ```
  # https://workflowy.com/#/daa0961ddeee · 2026-09-24 12:00 에 Workflowy 에서 읽음 · 노드 42개
  - 인증 개편  (id: daa0961ddeee)
    - [bullets] 인증 미들웨어 3단계 분리  (id: 1a2b3c4d5e6f)
      │ 2026-09-23 14:05
      - ✓ [todo] Phase 1 · 구조 파악  (id: 6f5e4d3c2b1a)
  ```
  한 줄에 노드 하나다. `✓` 는 완료, `[ ]` 는 layoutMode 원문(없으면 생략), 들여쓰기가 깊이, `┆` 는 여러 줄 제목(코드 블록 등)의
  나머지, `│` 는 note. 제목·note 는 HTML 을 평문으로만 바꾼다 (서식은 빠지고 링크는 `글자 (주소)`).
  하위를 다 읽지 못한 노드에는 `[하위 못 읽음]` 이 붙고 결과에 이유가 나온다.
- 진행 상태는 `${CLAUDE_PLUGIN_DATA}/snapshot/<session id>-<short id>.json` 에 있고, 결과를 전하면 지운다.
  같은 노드를 읽는 중이면 새로 띄우지 않고 알린다. 프로세스가 결과 없이 사라지면 "중단됨" 으로 알린다.

### 동작 방식

| 구성 | 역할 |
|---|---|
| MCP 서버 `workflowy` (`scripts/mcp.py`) | Claude 가 쓰는 도구 `create`(노드 추가. `request: true` 면 요청 노드), `close`(todo 닫기: 완료·취소·방향 전환·보류), `read`(본문 읽기. 데이터 폴더의 cache·state 를 읽기만 한다). 수정·삭제 도구는 없다 |
| PreToolUse 훅 `guard` | 기록 중인 세션의 메인 Claude 가, root 또는 이 세션에서 만들었거나 이어받은 노드 아래에만 쓰도록 검사. root 바로 아래는 요청만. todo 를 닫을 때 결과·이유·하위 todo 도 검사. `read` 도 같은 범위만. 범위 안이면 권한 확인 없이 허용 |
| PostToolUse 훅 `track` | 만든 노드와 닫은 todo(보류 포함, 닫은 시각)를 state 에 기록. 끝난 `sync` 결과가 있으면 도구 결과 뒤에 덧붙여 전함 |
| PreToolUse 훅 `step` | 도구 실행을 지금 작업 중인 노드 아래에 `▹` 로 추가 |
| UserPromptSubmit 훅 `prompt` | `/workflowy:workstream` 인자 처리와 이어받기(시작 때 요청 목록 읽기, `sync` 때 백그라운드 프로세스 띄우기, `clear-cache` 때 cache 비우기). 기록 중이면 요청마다 기록 지침을 한 줄로 상기하고, 끝난 `sync` 결과를 전함 |
| 백그라운드 `wf.py sync-run` | `sync` 가 띄우는 분리된 프로세스. root 아래 전체를 끝까지 읽어 cache 를 통째로 바꾸고, 잠금을 잠깐 잡아 state 에 합친 뒤 결과를 남김 |
| SessionStart 훅 | 대화 압축·재개 뒤 기록 중인 root 와 지금까지 만든 노드(id 포함)를 Claude 에게 다시 알림 |
| UserPromptSubmit 훅 `snapshot.py` | `/workflowy:snapshot <id>` 면 백그라운드 읽기를 띄운다. 끝났는데 전하지 않은 스냅숏 결과가 있으면 전함. workstream 과 따로 돈다 |
| 백그라운드 `snapshot.py run` / `wait` | `run` 은 노드와 하위 전체를 끝까지 읽어 파일로 저장하고 결과를 남김. `wait` 는 Claude 가 Bash 로 그 끝을 기다려 결과를 받음 (Workflowy 를 부르지 않음) |

- state 는 `stop` 하거나 다른 노드로 바꾸면 `<session id>.<시각>.json` 으로 남겨 둔다. 다른 세션이 이어받을 때
  그 세션이 cache 를 받은 뒤에 만든 노드와 닫은 todo 를 여기서 읽는다.
- cache 는 root 마다 하나이고, 두 세션이 함께 `sync` 하면 나중에 읽기 시작한 쪽이 남는다. 깨진 cache 는 비운 것으로 보고 알린다.
- `sync` 의 진행 상황과 결과는 `${CLAUDE_PLUGIN_DATA}/sync/<session id>.json` 에 있다. 세션당 하나만 돌고,
  프로세스가 결과 없이 사라지면 다음 확인 때 "중단됨" 으로 알린다. 도중에 기록을 멈추거나 root 를 바꾸면 cache 만 바꾸고
  이 세션의 state 에는 합치지 않는다.
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
(그 PC 에 그 세션들의 state 가 남아 있으면).
3.1 부터 요청은 h2 대신 굵은 bullets 로 쓰고, 제목 서식(h1·h2·h3)은 쓰지 않는다.
3.2 부터 todo 는 `complete` 대신 `close` 로 닫고, 취소·방향 전환·보류를 구별해 남긴다.
그 전에 완료한 todo 는 이어받을 때 완료(✓)로 보인다.
3.3 부터 요청은 `request: true` 로 만들고 root 바로 아래에는 요청만 들어간다. 요청 note 에는 날짜·시각만 자동으로 붙는다
(작업 디렉터리 이름은 더 쓰지 않는다). 그 전의 h2·굵은 bullets 요청은 이어받을 때 그대로 요청으로 본다.
3.4 부터 이어받기는 Workflowy 를 읽는다. 시작할 때는 요청 목록만, `sync` 로는 root 아래 전체를 백그라운드에서 읽는다.
root 바로 아래 노드는 모두 요청으로 보고, 닫은 방식(취소·변경)과 보류는 `close` 가 남긴 이유 노드로 알아본다.
끝나지 않은 todo 는 Claude 가 사용자에게 묻는다. 이름은 평문으로 보내고(요청 굵게만 유지) 꺾쇠는 `‹ ›` 로 바꾼다.
prompt 훅 timeout 은 30초가 되었다.
3.5 부터 이 PC 에 root 마다 cache 를 두고, `sync` 가 그것을 통째로 바꾼다 (전에는 세션 state 들의 합집합이 cache 노릇을 해서
`sync` 뒤에도 끝난 세션의 낡은 사본 — Workflowy 에서 지운 노드, 체크를 푼 todo — 가 다음 시작에 되살아났다).
`clear-cache` 가 생겼다. 올라온 뒤 root 마다 첫 `sync`(또는 `clear-cache`) 전까지는 전처럼 state 들을 모두 합쳐 이어받는다.
3.6 부터 cache 가 본문(제목 전체·note, 코드 원문)을 담고, Claude 가 `read` 도구로 읽는다. `sync` 는 단락·인용·코드와
이유 노드의 하위도 읽는다 (호출이 그만큼 는다). 올라온 뒤 root 마다 첫 `sync` 전까지 cache 의 노드에는 본문이 없다.
3.7 부터 `/workflowy:snapshot` 이 생겼다 (위 **노드 스냅숏**). workstream 의 동작은 그대로다.

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
