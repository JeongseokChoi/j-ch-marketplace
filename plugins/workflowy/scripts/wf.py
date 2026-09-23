#!/usr/bin/env python3
"""
wf.py - workflowy 플러그인의 훅. 기록 내용은 Claude 가 MCP 도구(mcp.py)로 직접 쓰고, 훅은 그 주변을 맡는다.

  prompt         UserPromptSubmit  /workflowy:session-log <id> | stop | doctor | (없음: 상태)
  guard          PreToolUse        workflowy 도구 호출 검사 — root 아래, 이 세션이 만든 노드만 허용
  track          PostToolUse       workflowy 도구가 만든 노드·완료를 세션 상태에 기록
  step           PreToolUse        그 밖의 도구 실행을 지금 작업 중인 노드 아래에 자동으로 붙인다
  session-start  SessionStart      resume/compact 뒤 기록 중이라는 사실과 노드 구조를 다시 알려준다

API key 와 상태 저장 위치는 훅 프로세스에만 넘어온다(CLAUDE_PLUGIN_OPTION_*, CLAUDE_PLUGIN_DATA).
"""
import io, json, os, re, sys, time, pathlib, urllib.error
from contextlib import redirect_stdout
from datetime import datetime
import wfapi
from wfapi import short

DATA   = os.environ.get("CLAUDE_PLUGIN_DATA")
STATE  = pathlib.Path(DATA) if DATA else None       # 업데이트에도 보존되는 플러그인 데이터 영역
ERRLOG = STATE / "error.log" if STATE else None
SKILL  = re.compile(r"^/(?:workflowy:)?session-log\b\s*(.*)$", re.S)   # 사용자가 직접 입력한 스킬
TOOL   = "mcp__plugin_workflowy_workflowy__"          # 플러그인 MCP 서버 도구 이름의 접두사
# Claude Code 가 스스로 넣는 턴(에이전트 보고, 완료 알림 등). source 필드가 없는 버전은 내용으로 판단한다.
SYSTEM = re.compile(r"\s*(<(agent-message|task-notification|system-reminder|local-command-caveat)\b"
                    r"|Another Claude session sent a message:|\[SYSTEM NOTIFICATION)")
HEADS  = ("h1", "h2", "h3")

# ----------------------------------------------------------------- 상태


def spath(sid): return STATE / "state" / f"{sid}.json"

def load(sid):
    try:    return json.loads(spath(sid).read_text(encoding="utf-8"))
    except Exception: return {}

def save(sid, st):
    p = spath(sid); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def lock(sid):
    """훅이 동시에 돌 때 상태 파일을 지킨다. 못 잡으면 None."""
    p = spath(sid).with_suffix(".lock"); p.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(200):                         # 최대 약 10초
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL))
            return p
        except FileExistsError:
            try:
                if time.time() - p.stat().st_mtime > 30:   # 시간 초과로 죽은 훅이 남긴 잠금
                    p.unlink()
            except OSError:
                pass
            time.sleep(0.05)
    return None


def norm(s): return " ".join(str(s or "").split())


def kids(st):
    """부모 short id -> 자식 노드 목록 (만든 순서 = 문서 순서. create 는 항상 맨 아래에 붙인다)."""
    out = {}
    for n in st.get("nodes") or []:
        out.setdefault(n["parent"], []).append(n)
    return out


def focus(st):
    """도구 실행을 붙일 노드. 트리 순서로 첫 번째 열린 todo 에서 시작해 그 아래 열린 todo 로 끝까지 내려간다.
    Phase 를 한꺼번에 만들어 두어도 지금 하는 Phase(그 안의 지금 하는 작업)에 붙는다.
    열린 todo 가 없으면 steps=true 로 만든 노드나 제목 중 마지막 것."""
    ch = kids(st)

    def walk(p):
        for n in ch.get(p, []):
            if n["type"] == "todo":
                if n.get("done") or not n.get("steps", True):
                    continue
                return walk(short(n["id"])) or n
            r = walk(short(n["id"]))
            if r:
                return r
        return None

    f = walk(short(st.get("root")))
    if f:
        return f
    rest = [n for n in st.get("nodes") or [] if n["type"] != "todo"
            and (n.get("steps") is True or (n["type"] in HEADS and n.get("steps") is not False))]
    return rest[-1] if rest else None


def find(st, nid):
    s = short(nid)
    return next((n for n in st.get("nodes") or [] if short(n["id"]) == s), None) if s else None


def tree(st, limit=80):
    """Claude 가 이어서 쓸 수 있도록 지금까지 만든 노드를 id 와 함께 보여준다."""
    ch, lines = kids(st), []

    def walk(p, depth):
        for n in ch.get(p, []):
            mark = ("✓ " if n.get("done") else "☐ ") if n["type"] == "todo" else ""
            lines.append(f"{'  ' * depth}- {mark}[{n['type']}] {n['name']}  (id: {short(n['id'])})")
            walk(short(n["id"]), depth + 1)

    walk(short(st.get("root")), 0)
    if len(lines) > limit:
        lines = [f"… 앞의 {len(lines) - limit}줄 생략"] + lines[-limit:]
    return "\n".join(lines) or "(아직 만든 노드 없음)"


def summary(st):
    f = focus(st)
    return (f"기록 root: '{st.get('root_name')}' {wfapi.url(st['root'])}  (root id: {short(st['root'])})\n"
            f"지금 도구 실행이 붙는 노드: {f['name'] + ' (id: ' + short(f['id']) + ')' if f else '없음'}\n"
            f"지금까지 만든 노드:\n{tree(st)}")

# ----------------------------------------------------------------- /workflowy:session-log


REMIND = ("[workflowy] 이 세션은 Workflowy 에 기록 중이다. 이 요청도 진행하는 대로 root 아래에 정리해 쓴다 "
          "(단계는 todo, 단계의 발견·결과는 그 todo 아래에 쓰고 complete). "
          "도구 description 은 사용자의 언어로, 명사형으로 짧게 쓴다 — 그대로 기록된다.")


def h_prompt(ev, st, sid, arg):
    if arg is None:                              # 사용자 요청: 기록 중이면 지침을 짧게 상기시킨다
        by_user = ev.get("source", "user") == "user" and not SYSTEM.match(ev.get("prompt") or "")
        return REMIND if st.get("root") and by_user else None
    a = arg.strip()
    if a in ("", "status"):
        return "[workflowy] " + (summary(st) if st.get("root") else "기록 중인 세션이 없습니다.")
    if a == "stop":
        if not st.get("root"):
            return "[workflowy] 기록 중인 세션이 없습니다."
        spath(sid).unlink(missing_ok=True)
        return "[workflowy] 기록을 멈췄습니다. 이미 쓴 노드는 그대로 둡니다. 이 세션에서는 더 이상 workflowy 도구를 쓰지 않는다."
    if a == "doctor":
        buf = io.StringIO()
        with redirect_stdout(buf):
            do_doctor(st)
        return "[workflowy] 점검 결과\n" + buf.getvalue()
    s = short(a.split()[0])
    if not s:
        return (f"[workflowy] '{a}' 는 노드 id 가 아닙니다. Workflowy URL 끝 12자리나 URL 을 주세요 "
                "(예: /workflowy:session-log daa0961ddeee).")
    if st.get("root") and short(st["root"]) == s:
        return "[workflowy] 이미 이 노드에 기록 중입니다.\n" + summary(st)
    try:
        n = wfapi.get(s)
    except urllib.error.HTTPError as e:
        return "[workflowy] 기록을 시작하지 못했습니다: " + {
            401: "API key 가 잘못됨", 403: "권한 없음", 404: f"노드 {s} 를 찾을 수 없음"}.get(e.code, f"HTTP {e.code}")
    prev = st.get("root_name")
    st.clear()
    st.update(root=n["id"], root_name=norm(n.get("name"))[:80] or "(제목 없음)",
              started=time.time(), cwd=ev.get("cwd"), nodes=[])
    save(sid, st)
    out = (f"[workflowy] 기록 시작: '{st['root_name']}' {wfapi.url(n['id'])}\n"
           f"root id: {s} — 이 노드 자체는 건드리지 않고, 그 아래에 workflowy create 도구로 쓴다.")
    if prev:
        out += f"\n(이전에 기록하던 '{prev}' 대신 이 노드에 기록한다. 이전 노드들은 parent 로 쓸 수 없다.)"
    err = new_errors()
    if err:
        out += f"\n[workflowy] 지난 확인 이후 기록 오류 {len(err)}건. 마지막: {err[-1]} — 사용자에게 알린다."
    return out

# ----------------------------------------------------------------- workflowy 도구 호출 전후


def decide(ok, why=""):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow" if ok else "deny",
        "permissionDecisionReason": why}}, ensure_ascii=False))


def h_guard(ev, st):
    """범위 밖이면 막고, 범위 안이면 권한 확인 없이 허용한다. 모든 쓰기가 이 검사를 거친다."""
    if ev.get("agent_id"):
        return decide(False, "Workflowy 기록은 메인 세션만 한다. 서브에이전트는 결과를 보고만 한다.")
    if not st.get("root"):
        return decide(False, "이 세션은 Workflowy 에 기록 중이 아니다. 사용자가 /workflowy:session-log <id> 로 시작해야 쓸 수 있다.")
    i, tool = ev.get("tool_input") or {}, ev.get("tool_name", "")[len(TOOL):]
    if tool == "create":
        p = short(i.get("parent"))
        if p and (p == short(st["root"]) or find(st, p)):
            return decide(True)
        return decide(False, f"parent 는 기록 root({short(st['root'])}) 이거나 이 세션에서 create 로 만든 노드여야 한다. "
                             "지금까지 만든 노드:\n" + tree(st, 30))
    if tool == "complete":
        n = find(st, i.get("id"))
        if not n:
            return decide(False, "이 세션에서 create 로 만든 todo 만 완료 처리할 수 있다 (root 와 기존 노드는 건드리지 않는다).")
        if n["type"] != "todo":
            return decide(False, f"'{n['name']}' 은 todo 가 아니다 ({n['type']}).")
        if n.get("done"):
            return decide(False, f"'{n['name']}' 은 이미 완료되었다.")
        return decide(True)
    return decide(False, f"알 수 없는 workflowy 도구: {tool}")


ID = re.compile(r"(?:id|completed): ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def h_track(ev, st, sid):
    if ev.get("agent_id") or not st.get("root"):
        return
    i, tool = ev.get("tool_input") or {}, ev.get("tool_name", "")[len(TOOL):]
    m = ID.search(json.dumps(ev.get("tool_response"), ensure_ascii=False))
    if not m:
        return                                   # 실패한 호출
    if tool == "create":
        t = i.get("type") or "bullets"
        name = norm(str(i.get("name") or "").split("\n")[0]) if t != "code" else "(코드)"
        st["nodes"].append({"id": m.group(1), "parent": short(i.get("parent")), "type": t,
                            "name": name[:60] + ("…" if len(name) > 60 else ""),
                            **({"steps": bool(i["steps"])} if "steps" in i else {})})
    elif tool == "complete":
        n = find(st, m.group(1))
        if n:
            n["done"] = True
    save(sid, st)

# ----------------------------------------------------------------- 도구 실행 자동 기록


def step_of(ev):
    """붙일 문구. 없으면 None. 모든 도구마다 불리므로 상태 없이 판단한다."""
    if ev.get("agent_id"):
        return None                              # 서브에이전트가 부른 도구는 메인 세션의 단계가 아니다
    name = ev.get("tool_name") or ""
    if name.startswith(TOOL) or name == "Skill":
        return None
    i = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    if name in ("Edit", "Write", "NotebookEdit"):
        f = i.get("file_path") or i.get("notebook_path")
        return f"{'작성' if name == 'Write' else '편집'} · {pathlib.Path(f).name}" if f else None
    d = norm(i.get("description"))
    if not d:
        return None
    return ("\U0001f916 " if name in ("Agent", "Task") else "") + d


def h_step(ev, st, sid, d):
    if not st.get("root"):
        return None
    f = focus(st)
    if not f or st.get("last_step") == [f["id"], d]:
        return None                              # 같은 노드에 같은 문구가 이어지면 한 번만
    st["last_step"] = [f["id"], d]
    save(sid, st)
    return f["id"]                               # API 호출은 잠금을 푼 뒤에 한다

# ----------------------------------------------------------------- 세션 재개


def h_session_start(ev, st):
    if not st.get("root"):
        return None
    return ("[workflowy] 이 세션은 Workflowy 에 작업 기록 중이다 (/workflowy:session-log 스킬의 지침을 계속 따른다).\n"
            "- 지금 하는 작업은 workflowy create 도구로 root 아래에 정리해 쓰고, todo 는 끝나는 대로 complete 한다.\n"
            "- 도구 실행은 훅이 열린 todo 아래에 자동으로 붙인다. 도구의 description 은 명사형으로 짧게 쓴다.\n"
            "- 이미 쓴 노드는 고치거나 지우지 않고, 새 노드를 추가만 한다.\n" + summary(st))

# ----------------------------------------------------------------- 점검


def new_errors():
    """지난 확인 이후 쌓인 오류 줄. 기록 실패가 조용히 묻히지 않게 한 번씩 알린다."""
    seen = STATE / "error.seen"
    try:
        size = ERRLOG.stat().st_size
        done = int(seen.read_text()) if seen.exists() else 0
        if size <= done:
            return []
        with ERRLOG.open("rb") as f:
            f.seek(done if done <= size else 0)
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
        seen.write_text(str(size))
        return lines
    except OSError:
        return []


def do_doctor(st):
    ok = True
    key = wfapi.conf("WORKFLOWY_API_KEY", "api_key")
    print(f"  {'ok ' if key else 'FAIL'} API key      {'설정됨' if key else '없음'}")
    print(f"  ok  상태 저장    {STATE}")
    print(f"  ok  Python       {sys.version.split()[0]}")
    ok &= bool(key)
    if key:
        target = st.get("root") or "None"
        try:
            t0 = time.time()
            if st.get("root"):
                nm = wfapi.get(st["root"])["name"]
                print(f"  ok  API 연결     root '{nm}' ({time.time()-t0:.2f}초)")
            else:
                wfapi.call("GET", "/targets")
                print(f"  ok  API 연결     ({time.time()-t0:.2f}초)")
        except urllib.error.HTTPError as e:
            code = {401: "API key가 잘못됨", 403: "권한 없음",
                    404: f"root {target} 를 찾을 수 없음"}.get(e.code, f"HTTP {e.code}")
            print(f"  FAIL API 연결     {code}"); ok = False
        except Exception as e:
            print(f"  FAIL API 연결     {type(e).__name__}: {e}"); ok = False
    print(f"  --  기록 상태    " + (f"'{st['root_name']}' 에 기록 중, 만든 노드 {len(st['nodes'])}개"
                                  if st.get("root") else "기록 중 아님"))

    if ERRLOG.exists() and ERRLOG.stat().st_size:
        print(f"\n  주의: {ERRLOG} 에 기록된 오류가 있습니다 (마지막 5줄)")
        for ln in ERRLOG.read_text(encoding="utf-8").strip().split("\n")[-5:]:
            print("        " + ln)

    print("\n" + ("전부 정상." if ok else "문제가 있습니다. 위의 FAIL 항목을 확인하세요."))
    return 0 if ok else 1

# ----------------------------------------------------------------- 진입점


def log_error(mode, e):
    ERRLOG.parent.mkdir(parents=True, exist_ok=True)
    with ERRLOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%F %T} [{mode}] {type(e).__name__}: {e}\n")


def main():
    # Windows 에서 파이프로 연결된 표준 입출력은 로캘 인코딩(cp949 등)을 쓴다.
    # Claude Code 는 훅과 UTF-8 로 주고받으므로 명시적으로 맞춘다.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if not STATE:
        print("[workflowy] 플러그인 훅 밖에서 실행되어 API key 와 기록 상태를 쓸 수 없습니다.\n"
              "점검은 /workflowy:session-log doctor 로 하세요.", file=sys.stderr)
        sys.exit(1)
    try: ev = json.loads(sys.stdin.read() or "{}")
    except Exception: ev = {}
    sid = ev.get("session_id", "")

    # 모든 요청·도구마다 불리는 훅은 할 일이 없으면 잠금도 잡지 않고 끝낸다
    m = SKILL.match((ev.get("prompt") or "").strip()) if mode == "prompt" else None
    d = step_of(ev) if mode == "step" else None
    if (mode == "step" and not d) or \
       ((mode in ("step", "session-start") or (mode == "prompt" and not m)) and not spath(sid).exists()):
        sys.exit(0)

    say, target = None, None
    lk = lock(sid)
    try:
        st = load(sid)
        if   mode == "prompt":        say = h_prompt(ev, st, sid, m.group(1) if m else None)
        elif mode == "guard":         h_guard(ev, st)
        elif mode == "track":         h_track(ev, st, sid)
        elif mode == "step":          target = h_step(ev, st, sid, d)
        elif mode == "session-start": say = h_session_start(ev, st)
    except Exception as e:
        log_error(mode, e)
        if mode == "prompt" and m:
            say = f"[workflowy] 실패: {type(e).__name__}: {e}"
    finally:
        if lk:
            lk.unlink(missing_ok=True)
    if target:
        try: wfapi.create(target, "▹ " + wfapi.label(d, 200))
        except Exception as e: log_error(mode, e)
    if say:                                      # UserPromptSubmit·SessionStart 의 stdout 은 Claude 의 컨텍스트가 된다
        print(say)
    sys.exit(0)   # 훅은 무슨 일이 있어도 0. exit 2는 세션을 차단한다.


if __name__ == "__main__":
    main()
