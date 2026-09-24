#!/usr/bin/env python3
"""
mcp.py - Claude 가 Workflowy 작업 로그를 직접 쓰는 MCP 서버 (stdio, 표준 라이브러리만).

도구는 만들기(create), todo 닫기(close), 본문 읽기(read) 셋이다. 이미 쓴 노드를 고치거나 지우는 도구는 두지 않는다.
어느 노드 아래에 쓰고 어느 노드를 읽을 수 있는지는 state 를 아는 훅(wf.py guard)이 도구 호출 전에 검사한다.
서버는 세션을 모르므로 상태를 갖지 않는다. read 는 데이터 폴더의 cache·state 를 읽기만 한다 (wf.bodies).
"""
import json, os, pathlib, sys, urllib.error
import wfapi, wf

VERSION = "4.1.0"

# 데이터 폴더: plugin.json 이 env 로 넘기는 WORKFLOWY_DATA, 없으면 Claude Code 가 넘기는 CLAUDE_PLUGIN_DATA.
# 치환되지 않은 ${...} 는 없는 것으로 본다. 없으면 read 가 그렇다고 알린다.
for _k in ("WORKFLOWY_DATA", "CLAUDE_PLUGIN_DATA"):
    _v = os.environ.get(_k)
    if _v and not _v.startswith("${"):
        wf.DATA_DIR = pathlib.Path(_v)
        break

INSTRUCTIONS = (
    "사용자가 /workflowy:workstream <id> 로 기록을 시작한 세션에서만 쓴다. "
    "지정된 root 노드 자체는 건드리지 않고 그 아래에 노드를 추가만 한다. "
    "이전 기록의 본문은 read 로 root 아래에서만 읽는다.")

TOOLS = [
    {"name": "create",
     "description": (
         "Workflowy 노드를 parent 의 맨 아래에 추가하고 id 와 url 을 돌려준다. "
         "parent 는 기록 root 이거나 이 세션에서 만들었거나 이어받은 노드여야 한다. "
         "root 바로 아래에는 요청만 쓴다: 새 요청은 parent=root, request=true 로 만든다(제목은 굵게, note 첫 줄에 날짜·시각이 "
         "자동으로 붙는다). 요청 안에 쓸 노드는 parent 를 그 요청이나 하위 노드로 준다. "
         "방금 만든 노드 아래에 쓸 노드는 그 id 를 받은 뒤에 부른다. "
         "type: bullets(일반 항목. 요청 안의 큰 주제도 bullets 로 두고 하위를 들여 쓴다) | "
         "todo(체크할 작업, 끝나거나 멈추면 close) | p(단락) | quote-block(인용) | code(코드·명령·로그, name 에 코드 원문). "
         "name 은 평문이다: 서식은 붙지 않는다(요청 제목의 굵게만 서버가 붙인다). "
         "Workflowy 가 서식·태그로 읽는 글자는 서버가 비슷한 글자로 바꿔 보낸다(< > → ‹ ›, * → ∗, 백틱 → ˋ). "
         "code 가 아닌 type 에서 여러 줄 name 은 첫 줄만 제목이 되고 나머지는 note 로 간다. "
         "긴 설명은 note 에 넣는다."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "parent": {"type": "string", "description": "부모 노드 id (root 의 short id, create 가 돌려준 id, 기록 시작 때 받은 id)"},
             "name": {"type": "string", "description": "노드 제목. type=code 이면 코드 원문(여러 줄 가능)"},
             "type": {"type": "string", "enum": list(wfapi.TYPES), "default": "bullets"},
             "note": {"type": "string", "description": "제목 아래에 붙는 설명(여러 줄 가능). 마크다운은 그려지지 않는다"},
             "steps": {"type": "boolean",
                       "description": "todo 는 기본 true: 완료되기 전까지 이후 도구 실행이 이 노드 아래에 자동으로 붙는다. "
                                      "'나중에 확인' 처럼 지금 하는 작업이 아닌 todo 는 false. "
                                      "todo 가 아닌 노드에 true 를 주면 열린 todo 가 없을 때 도구 실행이 여기에 붙는다."},
             "request": {"type": "boolean",
                         "description": "root 바로 아래에 새 요청을 만들 때만 true (type 은 bullets). "
                                        "제목은 굵게, note 첫 줄에 날짜·시각이 자동으로 붙으니 직접 쓰지 않는다."}},
         "required": ["parent", "name"]}},
    {"name": "close",
     "description": (
         "이 세션에서 만들었거나 이어받은 todo 를 닫는다. 되돌리거나 다시 열 수 없다. outcome 으로 어떻게 닫는지 고른다: "
         "done(끝냄, 체크) | cancel(하지 않기로 함, 체크) | replace(계획이 바뀌어 다른 방법으로 대신함, 체크) | "
         "hold(나중에 이어서 함. 체크하지 않고, 이후 도구 실행도 붙지 않는다). "
         "reason 은 todo 아래에 '결과: …' '✕ 취소: …' '↪ 변경: …' '⏸ 보류: …' 로 쓰인다. "
         "cancel·replace·hold 는 reason 이 필수이고, done 도 그 todo 아래에 쓴 결과가 없으면 필수다. "
         "하지 않은 todo 를 done 으로 닫지 않는다. "
         "ids 에 여러 todo 를 주면 같은 outcome·reason 으로 각각 닫는다. 아직 닫지 않은 하위 todo 는 ids 에 함께 넣는다."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                     "description": "닫을 todo 의 id 들 (create 가 돌려줬거나 기록 시작 때 받은 id)"},
             "outcome": {"type": "string", "enum": list(wfapi.OUTCOMES), "default": "done"},
             "reason": {"type": "string", "description": "한 줄 결과(done) 또는 이유(cancel·replace·hold)"}},
         "required": ["ids"]}},
    {"name": "read",
     "description": (
         "이어받았거나 이 세션에서 만든 노드의 본문(제목 전체·note, code 는 코드 원문)을 하위 트리와 함께 돌려준다. "
         "이어받기 안내에는 제목(60자까지)만 보이므로, 이전 요청의 내용이 필요할 때 그 요청의 id 로 부른다. 필요한 노드만 읽는다. "
         "Workflowy 를 부르지 않고 이 PC 의 cache(마지막 sync 로 받은 트리)에 그 뒤 이 PC 의 세션들이 쓴 것을 더해 읽는다 — "
         "그 뒤 다른 PC 에서 쓰거나 Workflowy 에서 고친 내용은 없다. ▹ 도구 실행은 담지 않는다. "
         "root id 를 주면 root 아래 전체. 결과가 3만 자를 넘으면 거기서 멈추고 이어 읽을 노드의 id 를 알려 준다."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                     "description": "읽을 노드의 id 들 (기록 시작 때 받은 id, create 가 돌려준 id, root id)"},
             "depth": {"type": "integer", "minimum": 0,
                       "description": "하위를 몇 단계까지 보일지. 주지 않으면 끝까지, 0 은 그 노드만"}},
         "required": ["ids"]}},
]


def run(name, args):
    if name == "create":
        nid = wfapi.create(args.get("parent"), args.get("name"), args.get("type") or "bullets", args.get("note"),
                           request=args.get("request") is True)
        return f"id: {nid}\nurl: {wfapi.url(nid)}"
    if name == "close":
        return close(wfapi.ids_of(args.get("ids")), args.get("outcome") or "done", str(args.get("reason") or "").strip())
    if name == "read":
        ids, depth = wfapi.ids_of(args.get("ids")), args.get("depth")
        if not ids:
            raise ValueError("ids 가 비어 있음")
        return wf.bodies(ids, depth if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0 else None)
    raise ValueError(f"알 수 없는 도구: {name}")


def close(ids, outcome, reason):
    """todo 마다 이유 노드를 먼저 쓰고 체크한다(hold 는 체크하지 않는다). 훅(wf.py track)이 결과 줄을 읽어 상태에 반영한다.
    일부만 실패하면 id 별로 알리고, 모두 실패했을 때만 오류로 돌려준다."""
    if outcome not in wfapi.OUTCOMES:
        raise ValueError(f"지원하지 않는 outcome: {outcome} (가능: {', '.join(wfapi.OUTCOMES)})")
    if not ids:
        raise ValueError("ids 가 비어 있음")
    out, ok = [], 0
    for i in ids:
        try:
            if reason:
                nid = wfapi.create(i, wfapi.close_note(outcome, reason))
                out.append(f"note: {nid} under {i}")
            if outcome != "hold":
                wfapi.complete(i)
            out.append(f"closed: {i} {outcome}")
            ok += 1
        except urllib.error.HTTPError as e:
            out.append(f"failed: {i} HTTP {e.code} " + {404: "노드를 찾을 수 없음"}.get(e.code, e.reason or ""))
        except Exception as e:
            out.append(f"failed: {i} {type(e).__name__}: {e}")
    if not ok:
        raise RuntimeError("\n".join(out))
    return "\n".join(out)


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
