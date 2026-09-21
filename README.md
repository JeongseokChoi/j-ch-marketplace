# j-ch-marketplace

Claude Code 플러그인 모음.

## workflowy — Workflowy Session Logger

Claude Code 세션의 작업 내역을 Workflowy에 실시간으로 기록하고 보존한다.

### 설치

```bash
claude plugin marketplace add JeongseokChoi/j-ch-marketplace
claude plugin install workflowy@j-ch-marketplace \
  --config api_key=<WORKFLOWY_API_KEY> \
  --config root_id=<ROOT_NODE_SHORT_ID>
```

- API Key: https://workflowy.com/api-key 에서 발급
- root node short ID: Workflowy URL 끝 12자리 (`workflowy.com/#/daa0961ddeee` → `daa0961ddeee`)

### 사용

세션이 시작되면 자동으로 기록된다. 별도 조작이 필요 없다.

의미 단위 메모는 `/workflowy:session-log` 스킬이 맡는다. Claude가 계획·결정·막힌
지점을 알아서 남기고, 직접 시킬 수도 있다.

```
/workflowy:session-log note 인증 로직을 3단계로 분리하기로 함
/workflowy:session-log link      # 현재 세션 노드 링크
/workflowy:session-log close     # 강제 종료된 세션 수동 마감
/workflowy:session-log doctor    # 설정 점검
```

### 기록되는 구조

```
🤖 AI                                     ← root node (설치 시 지정)
└── ## my-api · 2026-09-21 19:30          세션 (종료 시 체크)
    │   note: cwd / session id / 시작 사유
    ├── ☑ 19:30 인증 미들웨어를 verify/extract/inject 3단계로 분리했습니다. · 11분
    │   │   note: 전체 프롬프트                 턴 (완료)
    │   ├── ▸ 계획: verify/extract/inject 3단계로 분리
    │   ├── ☑ 🤖 Explore: Find JWT verify callers · 2분    서브에이전트
    │   │       note: 에이전트에게 준 지시
    │   └── ▸ 막힘: jwt v9에서 verify() 시그니처 변경
    ├── ☐ 19:41                              턴 (진행 중: 시각만)
    │       note: 전체 프롬프트
    └── ⏹ 종료 · clear · 22분
```

턴 제목은 진행 중에는 시각만 두고, 턴이 끝나면 **Claude 응답의 첫 문장**으로 채운다.
요청 전문은 노트에 있다. 응답을 찾지 못하면 요청 앞부분을 제목으로 쓴다.

Workflowy에서 root node를 열어두면, **체크 안 된 맨 아래 항목이 "지금 하는 일"** 이다.
작업 중에 대기열에 넣은 요청도 바로 턴으로 추가되고, 그 요청이 실제로 처리된 턴이 끝날 때
체크된다. 처리되지 않은 턴은 다음처럼 표시된다.

| 제목 | 뜻 | 체크되는 시점 |
|---|---|---|
| ↳ 앞 요청과 함께 처리 | 앞 요청을 처리하는 도중에 전달되어 함께 처리됨 | 그 턴이 끝날 때 |
| 중단됨 | 작업 중 Esc 로 취소했거나 도구 실행을 거부함 | 입력 대기 알림 또는 다음 요청 중 먼저 오는 때 |
| 취소됨 | Claude 가 응답을 시작하기 전에 취소함 | 입력 대기 알림 또는 다음 턴이 끝날 때 |
| 처리되지 않음 | 대기열에 넣었지만 세션이 먼저 끝남 | 세션이 끝날 때 |

취소 시점에 실행되는 훅은 없다. 그래서 Claude Code 가 입력 없이 약 60초가 지나면 보내는
입력 대기 알림(`Notification` 의 `idle_prompt`)을 신호로 쓴다. 그 전까지는 취소된 턴이
진행 중처럼 보인다.

턴 아래의 `▸` 메모는 `session-log` 스킬로 남긴 것이고, `🤖` 항목은 Claude 가 띄운
서브에이전트다. 에이전트를 띄울 때 적은 설명이 제목, 지시 내용이 노트가 되며, 에이전트가 끝나면
소요 시간과 함께 체크된다. 도구 호출(실행한 명령, 수정한 파일 등)은 기록하지 않는다.

### 주의

- 프롬프트에 섞인 토큰류는 정규식으로 마스킹하지만 완전하지 않다.
  사내 토큰 형식은 `scripts/wf.py` 의 `SECRET` 패턴에 추가할 것.
- 기록 실패는 세션을 막지 않는다. 오류는 `${CLAUDE_PLUGIN_DATA}/error.log` 에 쌓인다.

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
