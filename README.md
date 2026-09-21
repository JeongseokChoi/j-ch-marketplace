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
    ├── ☐ 19:30 인증 미들웨어 리팩터링        턴 (진행 중이면 체크 해제)
    │   │   note: 전체 프롬프트 + 할 일 체크리스트
    │   ├── ▸ 계획: verify/extract/inject 3단계로 분리
    │   ├── $ npm test -- src/auth          note: 전체 명령
    │   ├── ✏️ src/auth/middleware.ts
    │   └── ▸ 막힘: jwt v9에서 verify() 시그니처 변경
    ├── ☑ 19:41 테스트 고쳐줘 · 4분 · 7회     턴 (완료)
    └── ⏹ 종료 · clear · 22분 · 도구 34회
```

Workflowy에서 root node를 열어두면, **체크 안 된 맨 아래 항목이 "지금 하는 일"** 이다.

기록되는 도구: `Edit` `Write` `NotebookEdit` `Bash` `Task` `WebFetch` `WebSearch` `TodoWrite`.
`Read`/`Grep`/`Glob` 은 노이즈라 제외한다 — 바꾸려면 `hooks/hooks.json` 의
`PostToolUse` matcher 를 수정.

### 주의

- Bash 명령과 프롬프트에 섞인 토큰류는 정규식으로 마스킹하지만 완전하지 않다.
  사내 토큰 형식은 `scripts/wf.py` 의 `SECRET` 패턴에 추가할 것.
- 파일 *내용* 은 기록하지 않는다. 경로만 남는다.
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
