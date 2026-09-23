#!/usr/bin/env python3
"""
mcp.py - Claude 가 Workflowy 작업 로그를 직접 쓰는 MCP 서버 (stdio, 표준 라이브러리만).

도구는 만들기(create)와 완료(complete) 둘뿐이다. 이미 쓴 노드를 고치거나 지우는 도구는 두지 않는다.
어느 노드 아래에 쓸 수 있는지는 세션 상태를 아는 훅(wf.py guard)이 도구 호출 전에 검사한다.
서버는 세션을 모르므로 상태를 갖지 않는다.
"""
import json, sys, urllib.error
import wfapi

VERSION = "2.0.0"

INSTRUCTIONS = (
    "사용자가 /workflowy:session-log <id> 로 기록을 시작한 세션에서만 쓴다. "
    "지정된 root 노드 자체는 건드리지 않고 그 아래에 노드를 추가만 한다.")

TOOLS = [
    {"name": "create",
     "description": (
         "Workflowy 노드를 parent 의 맨 아래에 추가하고 id 와 url 을 돌려준다. "
         "parent 는 기록 root 이거나 이 세션에서 create 로 만든 노드여야 한다. "
         "type: bullets(일반 항목) | todo(체크할 작업, 끝나면 complete) | h1/h2/h3(구획 제목) | "
         "p(단락) | quote-block(인용) | code(코드·명령·로그, name 에 코드 원문). "
         "name 은 마크다운(**굵게**, `코드`, [링크](url))을 쓸 수 있다. "
         "code 가 아닌 type 에서 여러 줄 name 은 첫 줄만 제목이 되고 나머지는 note 로 간다. "
         "긴 설명은 note 에 넣는다."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "parent": {"type": "string", "description": "부모 노드 id (root 의 short id 또는 create 가 돌려준 id)"},
             "name": {"type": "string", "description": "노드 제목. type=code 이면 코드 원문(여러 줄 가능)"},
             "type": {"type": "string", "enum": list(wfapi.TYPES), "default": "bullets"},
             "note": {"type": "string", "description": "제목 아래에 붙는 설명(여러 줄 가능). 마크다운은 그려지지 않는다"},
             "steps": {"type": "boolean",
                       "description": "todo 는 기본 true: 완료되기 전까지 이후 도구 실행이 이 노드 아래에 자동으로 붙는다. "
                                      "'나중에 확인' 처럼 지금 하는 작업이 아닌 todo 는 false. "
                                      "todo 가 아닌 노드에 true 를 주면 열린 todo 가 없을 때 도구 실행이 여기에 붙는다."}},
         "required": ["parent", "name"]}},
    {"name": "complete",
     "description": "이 세션에서 create 로 만든 todo 를 완료 처리한다. 되돌리거나 다시 열 수 없다.",
     "inputSchema": {
         "type": "object",
         "properties": {"id": {"type": "string", "description": "create 가 돌려준 todo 의 id"}},
         "required": ["id"]}},
]


def run(name, args):
    if name == "create":
        nid = wfapi.create(args.get("parent"), args.get("name"), args.get("type") or "bullets", args.get("note"))
        return f"id: {nid}\nurl: {wfapi.url(nid)}"
    if name == "complete":
        nid = wfapi.complete(args.get("id"))
        return f"completed: {nid}"
    raise ValueError(f"알 수 없는 도구: {name}")


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return                                    # 알림(notifications/*)에는 답하지 않는다
    if method == "initialize":
        reply(mid, {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "workflowy", "version": VERSION},
                    "instructions": INSTRUCTIONS})
    elif method == "ping":
        reply(mid, {})
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        try:
            text, err = run(params.get("name"), params.get("arguments") or {}), False
        except urllib.error.HTTPError as e:
            text, err = f"Workflowy API 오류 HTTP {e.code}: " + {
                401: "API key 가 잘못됨", 403: "권한 없음", 404: "노드를 찾을 수 없음"}.get(e.code, e.reason), True
        except Exception as e:
            text, err = f"{type(e).__name__}: {e}", True
        reply(mid, {"content": [{"type": "text", "text": text}], "isError": err})
    else:
        reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    # Windows 파이프는 로캘 인코딩(cp949 등)을 쓰므로 UTF-8 로 맞춘다. 줄바꿈 변환도 끈다.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        for m in msg if isinstance(msg, list) else [msg]:
            handle(m)


if __name__ == "__main__":
    main()
