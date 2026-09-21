---
name: session-log
description: 현재 Claude Code 세션의 Workflowy 작업 로그에 의미 있는 메모를 남기거나, 세션 노드 링크를 확인하거나, 세션을 수동으로 마감한다. 계획을 세웠을 때 / 중요한 결정이나 발견이 있을 때 / 막혔을 때 / 사용자가 "workflowy에 기록해"라고 할 때 사용한다.
argument-hint: [note <텍스트> | link | close]
---

# Workflowy 작업 로그

도구 호출과 파일 변경은 훅이 자동으로 기록한다.
이 스킬은 훅이 알 수 없는 **의미 단위 정보**만 기록한다.

현재 세션 ID는 `${CLAUDE_SESSION_ID}` 이다.

## 명령

메모 추가 — 진행 중인 턴 아래에 불릿으로 붙는다:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wf.py" note "${CLAUDE_SESSION_ID}" "계획: 인증을 3단계로 분리"
```

세션 노드 링크 확인:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wf.py" link "${CLAUDE_SESSION_ID}"
```

세션 수동 마감 — SessionEnd 훅이 뜨지 않았을 때(강제 종료 등):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wf.py" close "${CLAUDE_SESSION_ID}"
```

설정 점검:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wf.py" doctor
```

## 무엇을 기록할 가치가 있는가

**기록한다**: 착수 전 계획 / 방향을 바꾼 이유 / 예상 밖의 발견 /
막힌 지점과 그 원인 / 사용자가 내린 결정.

**기록하지 않는다**: 파일을 읽었다·명령을 실행했다 같은 사실(훅이 이미 적는다) /
한 줄짜리 진행 중계 / 최종 요약(대화에 이미 있다).

메모 하나는 한 문장. 길어지면 여러 개로 나눈다.
기록 사실 자체를 사용자에게 보고하지 말고, 조용히 남기고 하던 일을 계속한다.
