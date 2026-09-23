#!/usr/bin/env python3
"""
wf.py - workflowy 플러그인의 훅. 기록 내용은 Claude 가 MCP 도구(mcp.py)로 직접 쓰고, 훅은 그 주변을 맡는다.

  prompt         UserPromptSubmit  /workflowy:workstream <id> | sync | clear-cache | stop | doctor | (없음: 상태)
  guard          PreToolUse        workflowy 도구 호출 검사 — root 아래, 이 세션이 만들었거나 이어받은 노드만 허용
  track          PostToolUse       workflowy 도구가 만든 노드와 닫은 todo 를 state 에 기록
  step           PreToolUse        그 밖의 도구 실행을 지금 작업 중인 노드 아래에 자동으로 붙인다
  session-start  SessionStart      resume/compact 뒤 기록 중이라는 사실과 노드 구조를 다시 알려준다

state  세션 하나의 기록 상태 (state/<session id>.json): 기록 중인 root, 이 세션의 노드(이어받은 것과 만든 것).
cache  root 하나의 트리를 이 PC 가 아는 사본 (cache/<root>.json): sync 로 받은 트리와 받은 시각. 세션들이 함께 쓴다.

API key 와 데이터 폴더 위치는 훅 프로세스에만 넘어온다(CLAUDE_PLUGIN_OPTION_*, CLAUDE_PLUGIN_DATA).
"""
import io, json, os, re, sys, time, pathlib, urllib.error
from contextlib import redirect_stdout
from datetime import datetime
import wfapi
from wfapi import short

DATA   = os.environ.get("CLAUDE_PLUGIN_DATA")
DATA_DIR = pathlib.Path(DATA) if DATA else None    # 업데이트에도 보존되는 플러그인 데이터 폴더 (state/ cache/ sync/)
ERRLOG = DATA_DIR / "error.log" if DATA_DIR else None
SKILL  = re.compile(r"^/(?:workflowy:)?workstream\b\s*(.*)$", re.S)    # 사용자가 직접 입력한 스킬
TOOL   = "mcp__plugin_workflowy_workflowy__"          # 플러그인 MCP 서버 도구 이름의 접두사
LIMIT  = 20       # doctor 가 트리 읽기를 잴 때의 시간 한도(초). prompt 훅 timeout(hooks.json, 30초) 안에 끝나야 한다
# Claude Code 가 스스로 넣는 턴(에이전트 보고, 완료 알림 등). source 필드가 없는 버전은 내용으로 판단한다.
SYSTEM = re.compile(r"\s*(<(agent-message|task-notification|system-reminder|local-command-caveat)\b"
                    r"|Another Claude session sent a message:|\[SYSTEM NOTIFICATION)")

# ----------------------------------------------------------------- state


def spath(sid): return DATA_DIR / "state" / f"{sid}.json"

def load(sid):
    try:    return json.loads(spath(sid).read_text(encoding="utf-8"))
    except Exception: return {}

def save(sid, st):
    p = spath(sid); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def put(p, obj):
    """파일을 통째로 바꾼다 (읽는 쪽이 반쯤 쓴 파일을 보지 않게). Windows 는 읽는 중이면 바꾸기가 실패해 몇 번 다시 한다."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    for _ in range(50):
        try:
            os.replace(tmp, p)
            return True
        except PermissionError:
            time.sleep(0.05)
    tmp.unlink(missing_ok=True)
    return False


def lock(sid):
    """훅이 동시에 돌 때 state 를 지킨다. 못 잡으면 None."""
    return lock_file(spath(sid).with_suffix(".lock"))


def lock_file(p):
    p.parent.mkdir(parents=True, exist_ok=True)
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


def archive(sid, st):
    """state 를 지우지 않고 <sid>.<시각>.json 으로 남긴다. 나중에 같은 root 로 시작한 세션이 이 세션이 쓴 것을 이어받는다."""
    p = spath(sid)
    if st.get("nodes"):
        p.replace(p.with_name(f"{sid}.{int(time.time() * 1000)}.json"))
    else:
        p.unlink(missing_ok=True)


def states(sid, root):
    """이 PC 의 다른 세션들(멈춘 세션 포함) 가운데 같은 root 에 기록한 state."""
    for p in (DATA_DIR / "state").glob("*.json"):
        if p == spath(sid):
            continue
        try:    o = json.loads(p.read_text(encoding="utf-8"))
        except Exception: continue
        if short(o.get("root")) == short(root) and o.get("nodes"):
            yield o


def from_states(sid, root):
    """3.4 까지의 이어받기: 이 PC 의 다른 세션들의 state 를 이어받은 노드까지 모두 합친다.
    cache 가 없을 때(이 PC 에서 아직 sync 하지 않은 root)만 쓴다. 끝난 세션의 낡은 사본도 섞인다.
    create 는 늘 맨 아래에 붙이므로 만든 시각 순이 곧 문서 순서다. 시각이 없는 노드는 그 세션의 시작 시각으로 본다.
    이어받은 세션도 그 노드를 갖고 있으므로 한 노드가 여러 파일에 있을 수 있다.
    어느 쪽이든 닫았으면 닫힌 것, 그렇지 않고 어느 쪽이든 보류했으면 보류."""
    got = []
    for o in states(sid, root):
        t0 = o.get("started") or 0
        got += [(n.get("t", t0), t0, i, n) for i, n in enumerate(o["nodes"])]
    out, seen = [], {}
    for t, _, _, n in sorted(got, key=lambda g: g[:3]):
        k = short(n["id"])
        if k in seen:
            if n.get("done"):
                seen[k].update(done=True, **({"outcome": n["outcome"]} if n.get("outcome") else {}))
                seen[k].pop("held", None)
            elif n.get("held") and not seen[k].get("done"):
                seen[k]["held"] = True
            continue
        seen[k] = dict(n, t=t, old=True)
        out.append(seen[k])
    return out

# ----------------------------------------------------------------- cache
# cache 는 root 하나의 트리를 이 PC 가 아는 사본이다: sync 로 Workflowy 에서 받은 트리(nodes)와 받은 시각(since).
# root 마다 파일 하나를 이 PC 의 세션들이 함께 쓴다. 쓰는 것은 sync(통째로 교체)와 clear-cache(비움)뿐이다.
# 받은 뒤에 세션들이 쓴 노드와 닫은 todo 는 각 state 에 있으므로 이어받을 때 더한다 (from_cache).
# 원본은 Workflowy 라 언제든 비우고 다시 받을 수 있다. 다만 sync 는 옛 cache 에서 steps(Workflowy 에 없음)와
# 못 읽은 하위를 이어 오므로, 잘못 들어간 값은 clear-cache 로만 끊을 수 있다.


def cpath(root): return DATA_DIR / "cache" / f"{short(root)}.json"


def load_cache(root):
    """(cache, 알릴 글). 파일이 없으면 None — 이 PC 에서 이 root 를 sync 한 적이 없다.
    깨졌으면 비운 것으로 본다 (받은 시각은 파일을 쓴 시각)."""
    p = cpath(root)
    try:
        c = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(c, dict) or not isinstance(c.get("nodes"), list):
            raise ValueError("nodes 가 없다")
        return c, None
    except FileNotFoundError:
        return None, None
    except Exception as e:
        try:    since = p.stat().st_mtime
        except OSError: since = time.time()
        return ({"root": root, "since": since, "by": "broken", "nodes": []},
                f"cache 파일이 깨져 비운 것으로 봤다({type(e).__name__}). "
                "하위가 필요하면 사용자에게 /workflowy:workstream sync 를 권한다.")


def save_cache(root, c, newer_than=None):
    """cache 를 통째로 바꾼다. newer_than 을 주면, 그보다 나중에 받거나 비운 cache 가 이미 있을 때 두고 False.
    파일을 바꾸지 못하면 OSError."""
    lk = lock_file(cpath(root).with_suffix(".lock"))
    try:
        if newer_than is not None:
            cur, broken = load_cache(root)
            if cur and not broken and (cur.get("since") or 0) > newer_than:
                return False
        if not put(cpath(root), c):
            raise OSError("cache 파일을 바꾸지 못했다 (다른 프로세스가 읽는 중)")
        return True
    finally:
        if lk:
            lk.unlink(missing_ok=True)


def cache_age(c):
    """cache 를 언제 받았는지(비웠는지) 한 마디."""
    if c is None:
        return "cache 없음"
    when = f"{datetime.fromtimestamp(c.get('since') or 0):%m-%d %H:%M}"
    return {"sync": f"마지막 sync {when}", "clear-cache": f"{when} 에 비움"}.get(
        c.get("by"), f"깨진 파일이라 비운 것으로 봄 ({when})")


def from_cache(sid, root):
    """(이어받을 노드, cache, 알릴 글). cache 에, 받은(비운) 뒤 이 PC 의 다른 세션들이 쓴 것을 더한다:
    그 뒤에 만든 노드와 그 뒤에 닫은 todo. 세션들이 이어받은 노드(old)는 읽지 않는다 — cache 보다 낡은 사본이다.
    cache 가 없으면 3.4 까지처럼 state 들을 모두 합친다 (from_states)."""
    c, warn = load_cache(root)
    if c is None:
        return from_states(sid, root), None, warn
    out = [dict(n, old=True) for n in c["nodes"]]
    byid = {short(n["id"]): n for n in out}
    since, new, closed = c.get("since") or 0, [], []
    for o in states(sid, root):
        t0 = o.get("started") or 0
        for n in o["nodes"]:
            if not n.get("old") and n.get("t", t0) >= since:
                new.append((n.get("t", t0), n))
            if (n.get("done") or n.get("held")) and n.get("ct", 0) >= since:
                closed.append(n)
    for _, n in sorted(new, key=lambda x: x[0]):          # 만든 순서 = 문서 순서
        k = short(n["id"])
        if k not in byid:                                 # sync 가 읽기 전에 만들어 이미 cache 에 있으면 그대로
            byid[k] = dict(n, old=True)
            out.append(byid[k])
    for n in sorted(closed, key=lambda n: n["ct"]):
        x = byid.get(short(n["id"]))
        if not x or x.get("done"):
            continue
        if n.get("done"):
            x.update(done=True, **({"outcome": n["outcome"]} if n.get("outcome") else {}))
            x.pop("held", None)
        else:
            x["held"] = True
    return out, c, warn


MODES  = ("bullets", "todo", "p", "quote-block", "h1", "h2", "h3")   # 그대로 받는 layoutMode (code-block 은 code)
REASON = {v: k for k, v in wfapi.OUTCOMES.items()}                 # close 가 쓴 이유 노드의 머리말 -> outcome
REASON_RE = re.compile("(" + "|".join(map(re.escape, REASON)) + "): ")
STAMP  = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")               # 요청 note 첫 줄의 날짜·시각 (wfapi.request_note)


def reason_of(name):
    m = REASON_RE.match(name)
    return REASON[m.group(1)] if m else None


def from_api(root, t, cached, mark=True):
    """Workflowy 에서 읽은 트리(wfapi.subtree)를 이어받을 노드 목록으로. priority 순 DFS 로 둔다 —
    kids() 가 목록 순서를 문서 순서로 본다. ▹ 도구 실행 노드는 넣지 않는다.
    Workflowy 에 없는 steps 는 cache 에서(cached, from_cache) 가져오고, 닫은 방식·보류는 close 가 쓴 이유 노드로 되살린다.
    하위를 읽지 못한 노드(t["missing"])는 그 아래를 cache 로 채우고, mark 면 partial 로 표시한다."""
    ch = {}
    for n in t["nodes"]:
        n["_name"] = norm(wfapi.unhtml(n.get("name")))
        ch.setdefault(short(n.get("parent_id")), []).append(n)
    for v in ch.values():
        v.sort(key=lambda n: n.get("priority") or 0)
    mine, lk, top = {short(n["id"]): n for n in cached}, kids({"nodes": cached}), short(root)
    missing, out = {short(x) for x in t["missing"]}, []

    def conv(n, parent):
        mode = (n.get("data") or {}).get("layoutMode") or "bullets"
        typ = "code" if mode == "code-block" else mode if mode in MODES else "bullets"
        x = {"id": n["id"], "parent": parent, "type": typ, "t": n.get("createdAt") or 0, "old": True,
             "name": "(코드)" if typ == "code" else clip(n["_name"])}
        if parent == top:
            x["request"] = True
            d = STAMP.match(n.get("note") or "")
            if d:
                x["at"] = d.group(0)
        if typ == "bullets" and reason_of(n["_name"]):
            x["by"] = reason_of(n["_name"])
        if typ == "todo":
            rs = [r for r in (reason_of(c["_name"]) for c in ch.get(short(n["id"]), [])) if r]
            if n.get("completed"):
                x["done"] = True
                closed = [r for r in rs if r != "hold"]
                if closed:
                    x["outcome"] = closed[-1]
            elif "hold" in rs:
                x["held"] = True
        m = mine.get(short(n["id"]))
        if m and "steps" in m:
            x["steps"] = m["steps"]
        return x

    def walk(p):
        for n in ch.get(p, []):
            if wfapi.tool_run(n):
                continue
            x = conv(n, p)
            out.append(x)
            if short(n["id"]) in missing:
                if mark:
                    x["partial"] = True
                out.extend(dict(d, old=True) for d in below(lk, x))
            else:
                walk(short(n["id"]))

    walk(top)
    return out


def read(root, cached, full, progress=None):
    """Workflowy 에서 읽어 이어받을 노드와 Claude 에게 알릴 한 줄. 읽지 못하면 노드 대신 None.
    full 이면 root 아래 전체를 끝까지(백그라운드 sync), 아니면 root 의 자식(요청 목록)만 읽고 하위는 cache(cached)로
    채운다(기록 시작). 전체는 노드마다 호출하므로 오래 걸린다 — 그래서 시작은 가볍게, 전체는 훅 밖에서 읽는다."""
    try:
        if full:
            t = wfapi.subtree(root, None, progress=progress)
        else:
            top = wfapi.children(root)
            t = {"nodes": top, "missing": [n["id"] for n in top], "errors": 0}
    except Exception as e:
        return None, f"Workflowy 에서 {'트리를' if full else '요청 목록을'} 읽지 못했다({wfapi.why(e)})."
    nodes = from_api(root, t, cached, mark=full)
    part = [n for n in nodes if n.get("partial")]
    if not part:
        return nodes, None
    # 긴 트리는 요약(outline)에 요청 목록과 마지막 요청만 나와 '하위 일부' 표시가 안 보이므로 여기서 직접 적는다
    names = ", ".join(f"'{n['name']}' ({short(n['id'])})" for n in part[:10])
    reasons = ", ".join(f"{k} {v}번" for k, v in sorted((t.get("reasons") or {}).items(), key=lambda x: -x[1]))
    return nodes, (f"호출 실패({t['errors']}번{': ' + reasons if reasons else ''})로 {len(part)}개 노드의 하위를 "
                   f"읽지 못해 cache 로 채웠다: {names}"
                   + (f" 외 {len(part) - 10}개" if len(part) > 10 else "") + ".")


def pending(nodes):
    """이어받은 노드에 끝나지 않은 todo 가 있으면 사용자에게 물으라는 안내."""
    o, h = sum(1 for x in nodes if is_open(x)), sum(1 for x in nodes if is_held(x))
    if not (o or h):
        return ""
    return (f"\n끝나지 않은 todo 가 있다 (☐ 열림 {o}개, ⏸ 보류 {h}개). 사용자가 Workflowy 에 직접 적은 것일 수도 있으니 "
            "혼자 판단해 닫거나 이어서 하지 않는다. 먼저 목록을 보여 주고 각각 어떻게 할지 사용자에게 묻는다: "
            "이어서 하기(그 아래에 새 todo) / 끝난 것으로 닫기(close done, 결과) / 취소(cancel) / 보류(hold) / 그대로 두기. "
            "사용자가 이미 지시했으면 묻지 않고 따른다. 보류된 todo 를 방법을 바꿔 하기로 하면 close(replace, 이유) 후 새 요청으로.")


# ----------------------------------------------------------------- sync (백그라운드)
# /workflowy:workstream sync 는 훅 timeout 에 묶이지 않도록 훅이 분리된 프로세스(wf.py sync-run)를 띄우고 곧바로 끝난다.
# 그 프로세스가 끝까지 읽어 cache 를 통째로 바꾸고, 잠금을 잠깐 잡아 state 에 합친 뒤 결과를 작업 파일에 남긴다.
# 외부 프로세스가 세션에 직접 알릴 방법은 없으므로, 결과는 다음에 불리는 훅이 Claude 에게 전한다:
# 사용자의 다음 메시지(prompt) 또는 workflowy 도구 뒤(track, PostToolUse 추가 문맥).


def jpath(sid): return DATA_DIR / "sync" / f"{sid}.json"


def load_job(sid):
    try:    return json.loads(jpath(sid).read_text(encoding="utf-8"))
    except Exception: return {}


def save_job(sid, job): put(jpath(sid), job)


def alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259                           # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def job_state(job):
    """running 인데 프로세스가 없으면 중단된 것으로 본다. 띄운 직후 pid 를 적기 전(1분 안)은 도는 중으로 본다."""
    s = job.get("status")
    if s == "running" and not alive(job.get("pid")) and not (not job.get("pid") and time.time() - job.get("started", 0) < 60):
        return "dead"
    return s


def progress_line(job):
    return (f"노드 {job.get('read', 0)}개 읽음, 호출 {job.get('calls', 0)}번, "
            + (f"한도(429) 대기 {job['waits']}번, " if job.get("waits") else "")
            + f"{int(time.time() - job.get('started', time.time()))}초째")


def start_sync(st, sid, spawn=None):
    """prompt 훅: 백그라운드 sync 를 띄우고 곧바로 돌아온다. 이미 돌고 있으면 진행 상황만 알린다.
    앞 sync 의 결과를 아직 전하지 않았으면 그 글을 먼저 붙인다 — 작업 파일을 새 job 으로 바꾸면 사라지기 때문이다.
    작업 파일은 한 번만 읽고 그것으로 판단한다 (따로 읽으면 그 사이 앞 sync 가 끝나 결과를 놓칠 수 있다)."""
    job = load_job(sid)
    if job_state(job) == "running":
        return f"[workflowy] sync 가 이미 돌고 있다 ({progress_line(job)}). 끝나면 결과를 알린다."
    say = [x for x in (news(job),) if x]
    save_job(sid, {"status": "running", "root": st["root"], "started": time.time(), "pid": None})
    try:
        (spawn or launch)(sid)
    except Exception as e:
        save_job(sid, {"status": "failed", "root": st["root"], "started": time.time(), "delivered": True})
        return "\n".join(say + [f"[workflowy] sync 를 시작하지 못했다 ({type(e).__name__}: {e}). cache 와 state 는 그대로다."])
    return "\n".join(say + [
        "[workflowy] sync 를 백그라운드에서 시작했다: Workflowy 에서 root 아래 전체를 끝까지 읽어 cache 를 새로 받는다 (시간 제한 없음). "
        "끝나면 사용자의 다음 메시지나 workflowy 도구 결과 뒤에 결과를 알린다. 그 전까지는 지금 기록으로 일하고, "
        "sync 결과(다른 PC 의 기록, Workflowy 에서 고친 내용)가 필요한 일은 결과가 온 뒤에 한다. "
        "진행 상황은 /workflowy:workstream 로 볼 수 있다고 사용자에게 알린다."])


def launch(sid):
    """wf.py sync-run <sid> 를 분리된 프로세스로 띄운다. 표준 입출력을 끊어야 Claude Code 가 파이프를 기다리지 않는다.
    API key 와 CLAUDE_PLUGIN_DATA 는 환경변수로 그대로 넘어간다 (Claude 에게는 보이지 않는다)."""
    import subprocess
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "sync-run", sid], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)


def merge(st, nodes, since):
    """sync 로 읽은 노드(nodes)를 state 에 합친다. Workflowy 에서 지워진 노드는 빠진다.
    이 세션이 만든 노드는 track 이 기록한 그대로 두되(steps·닫은 방식) Workflowy 에서 체크된 것은 반영하고,
    읽기 시작(since) 뒤에 만들어 읽은 트리에 없는 노드는 맨 뒤에 남긴다 (create 는 늘 맨 아래에 붙인다).
    이어받은 todo 를 읽기 시작 뒤에 이 세션이 닫았으면(ct) 읽은 트리가 그 전 모습이어도 닫은 쪽을 남긴다."""
    mine = {short(n["id"]): n for n in st["nodes"]}
    own = {k: n for k, n in mine.items() if not n.get("old")}
    merged, seen = [], set()
    for x in nodes:
        k = short(x["id"])
        seen.add(k)
        y = own.get(k)
        if y:
            y = dict(y)
            if x.get("done") and not y.get("done"):
                y.update(done=True, **({"outcome": x["outcome"]} if x.get("outcome") else {}))
                y.pop("held", None)
        elif not x.get("done") and mine.get(k, {}).get("ct", 0) >= since:
            z, x = mine[k], dict(x, ct=mine[k]["ct"])
            if z.get("done"):
                x.update(done=True, **({"outcome": z["outcome"]} if z.get("outcome") else {}))
                x.pop("held", None)
            elif z.get("held"):
                x["held"] = True
        merged.append(y or x)
    merged += [n for k, n in own.items() if k not in seen and n.get("t", 0) >= since]
    before, after = {short(n["id"]) for n in st["nodes"]}, {short(n["id"]) for n in merged}
    st["nodes"] = merged
    return len(after - before), len(before - after)


def run_sync(sid, reader=None):
    """wf.py sync-run: 끝까지 읽고(잠금 없이), 읽은 트리로 cache 를 통째로 바꾼 뒤 state 에 합친다(잠금 안에서).
    cache 는 기록을 멈췄거나 root 를 바꿨어도 바꾼다 — 세션이 아니라 root 의 것이다.
    결과 글은 작업 파일에 남겨 다음 훅이 전한다."""
    t0 = time.time()
    job = {"status": "running", "pid": os.getpid(), "started": t0, "read": 0, "calls": 0}
    first = load(sid)
    root = first.get("root")
    job["root"] = root
    save_job(sid, job)

    def progress(n, c, w=0):
        job.update(read=n, calls=c, waits=w)
        save_job(sid, job)

    try:
        if not root:
            raise RuntimeError("이 세션은 기록 중이 아니다")
        cached = {}
        for n in from_cache(sid, root)[0] + first.get("nodes", []):     # 같은 노드면 이 세션 것이 이긴다
            cached[short(n["id"])] = n
        nodes, warn = (reader or read)(root, list(cached.values()), True, progress)
        if nodes is None:
            raise RuntimeError(warn)
        try:
            # 이어받음(old)과 이번 읽기의 '하위 일부'(partial)는 세션의 표시다. cache 에는 트리만 남긴다
            fresh = [{k: v for k, v in n.items() if k not in ("old", "partial")} for n in nodes]
            kept = save_cache(root, {"root": root, "since": t0, "by": "sync", "nodes": fresh}, newer_than=t0)
            note = "" if kept else " cache 는 그 사이 다른 세션이 새로 받거나 비워서 그대로 두었다."
        except OSError as e:
            log_error("sync", e)
            note = f" cache 는 바꾸지 못했다({e})."
        lk = lock(sid)
        try:
            st = load(sid)
            if short(st.get("root")) != short(root):
                job.update(status="skipped", ended=time.time(),
                           message="sync 가 끝났지만 그 사이 기록을 멈췄거나 다른 노드로 바꿔 이 세션에는 합치지 않았다."
                                   + (note or " cache 는 새로 받았다."))
            else:
                added, gone = merge(st, nodes, t0)
                save(sid, st)
                job.update(status="done", ended=time.time(), read=len(nodes), message=(
                    f"sync 끝남 ({int(time.time() - t0)}초): Workflowy 에서 root 아래 전체를 읽어 cache 를 새로 받고 "
                    f"이 세션이 이어받은 부분을 바꿨다.{note} "
                    f"노드 {len(st['nodes'])}개 (새로 보인 노드 {added}개, 빠진 노드 {gone}개). 아래 트리로 흐름을 다시 파악한다.\n"
                    + outline(st) + pending([n for n in st["nodes"] if n.get("old")]) + ("\n" + warn if warn else "")))
        finally:
            if lk:
                lk.unlink(missing_ok=True)
    except Exception as e:
        job.update(status="failed", ended=time.time(), message=f"sync 실패: {str(e).rstrip('.')}. cache 와 state 는 그대로다.")
        log_error("sync", e)
    save_job(sid, job)


def news(job):
    """job 의 아직 전하지 않은 결과(끝남·실패·중단) 글. 없거나 아직 돌고 있으면 None."""
    if not job or job.get("delivered") or job_state(job) == "running":
        return None
    return "[workflowy] " + (job.get("message") or ("sync 프로세스가 결과 없이 멈췄다 (중단됨). cache 와 state 는 그대로다. "
                                                    "필요하면 사용자에게 /workflowy:workstream sync 를 다시 권한다."))


def sync_news(sid):
    """아직 전하지 않은 sync 결과가 있으면 그 글을 돌려주고 전한 것으로 표시한다."""
    job = load_job(sid)
    msg = news(job)
    if msg:
        job["delivered"] = True
        save_job(sid, job)
    return msg


def sync_status(sid):
    """상태 안내에 붙일 sync 한 줄."""
    job = load_job(sid)
    s = job_state(job)
    if not job:
        return ""
    if s == "running":
        return f"\nsync 진행 중: {progress_line(job)}."
    if s == "dead":
        return "\nsync 가 결과 없이 멈췄다 (중단됨)."
    return f"\n마지막 sync: {s} ({datetime.fromtimestamp(job.get('ended', job.get('started', 0))):%m-%d %H:%M})."


def norm(s): return " ".join(str(s or "").split())


def kids(st):
    """부모 short id -> 자식 노드 목록 (만든 순서 = 문서 순서. create 는 항상 맨 아래에 붙인다)."""
    out = {}
    for n in st.get("nodes") or []:
        out.setdefault(n["parent"], []).append(n)
    return out


def below(ch, n):
    """n 의 모든 하위 노드 (ch 는 kids 의 결과)."""
    for c in ch.get(short(n["id"]), []):
        yield c
        yield from below(ch, c)


def focus(st):
    """도구 실행을 붙일 노드. 트리 순서로 첫 번째 열린 todo 에서 시작해 그 아래 열린 todo 로 끝까지 내려간다.
    Phase 를 한꺼번에 만들어 두어도 지금 하는 Phase(그 안의 지금 하는 작업)에 붙는다.
    열린 todo 가 없으면 steps=true 로 만든 노드나 root 바로 아래 노드(요청) 중 마지막 것.
    이어받은 노드는 고르지 않는다. 새 요청을 만들기 전의 도구 실행이 이전 세션의 기록에 섞이지 않게 한다.
    보류된 todo 도 고르지 않는다. 이어받았거나 보류된 todo 의 아래에 이 세션이 만든 todo 는 고른다 (이어서 하는 방법)."""
    ch = kids(st)

    def walk(p):
        for n in ch.get(p, []):
            if n["type"] == "todo":
                if n.get("done") or not n.get("steps", True):
                    continue
                r = walk(short(n["id"]))
                if r or not (n.get("old") or n.get("held")):
                    return r or n
                continue
            r = walk(short(n["id"]))
            if r:
                return r
        return None

    f = walk(short(st.get("root")))
    if f:
        return f
    rest = [n for n in st.get("nodes") or [] if n["type"] != "todo" and not n.get("old")
            and (n.get("steps") is True or (is_request(st, n) and n.get("steps") is not False))]
    return rest[-1] if rest else None


def is_request(st, n):
    """요청 노드인가. 3.3 부터는 request=true 로 만든 노드. 그 전 기록은 root 바로 아래의 bullets·h1~h3 을 요청으로 본다
    (root 바로 아래의 p 같은, 잘못 들어간 노드는 요청이 아니다)."""
    if "request" in n:
        return bool(n["request"])
    return n["parent"] == short(st.get("root")) and n["type"] in ("bullets", "h1", "h2", "h3")


def last_request(st):
    """거부 안내에 보여 줄 지금 요청: 이 세션이 만든 마지막 요청, 없으면 이어받은 마지막 요청."""
    rs = [n for n in st.get("nodes") or [] if is_request(st, n)]
    mine = [n for n in rs if not n.get("old")]
    return (mine or rs or [None])[-1]


def find(st, nid):
    s = short(nid)
    return next((n for n in st.get("nodes") or [] if short(n["id"]) == s), None) if s else None


MARK = {"done": "✓ ", "cancel": "✕ ", "replace": "↪ "}     # 닫힌 todo. outcome 이 없는 것(3.1 이전)은 완료


def line(n, depth=0):
    mark = ""
    if n["type"] == "todo":
        mark = MARK.get(n.get("outcome"), "✓ ") if n.get("done") else "⏸ " if n.get("held") else "☐ "
    at = f", {n['at']}" if n.get("at") else ""
    part = "  [하위 일부: cache]" if n.get("partial") else ""
    return f"{'  ' * depth}- {mark}[{n['type']}] {n['name']}  (id: {short(n['id'])}{at}){part}"


def is_open(n): return n["type"] == "todo" and not n.get("done") and not n.get("held")
def is_held(n): return n["type"] == "todo" and not n.get("done") and n.get("held")


def lines(st, top=None, depth=0):
    """top(없으면 root) 아래 노드를 트리 순서로 한 줄씩."""
    ch, out = kids(st), []

    def walk(p, d):
        for n in ch.get(p, []):
            out.append(line(n, d))
            walk(short(n["id"]), d + 1)

    walk(short(top or st.get("root")), depth)
    return out


def tail(ls, limit):
    return [f"… 앞의 {len(ls) - limit}줄 생략"] + ls[-limit:] if len(ls) > limit else ls


def tree(st, limit=80):
    """Claude 가 이어서 쓸 수 있도록 지금까지 만든 노드를 id 와 함께 보여준다."""
    return "\n".join(tail(lines(st), limit)) or "(아직 만든 노드 없음)"


def outline(st, limit=80):
    """tree 와 같되, 여러 세션이 쌓여 길어지면 root 바로 아래 항목(요청), 열린 todo, 마지막 항목의 하위만 보여준다."""
    ls = lines(st)
    if len(ls) <= limit:
        return "\n".join(ls) or "(아직 만든 노드 없음)"
    root, byid = short(st["root"]), {short(n["id"]): n for n in st["nodes"]}
    top = kids(st).get(root, [])

    def under(n):                                # n 이 들어 있는 root 바로 아래 항목
        while n["parent"] != root and n["parent"] in byid:
            n = byid[n["parent"]]
        return n

    out = [f"root 바로 아래 {len(top)}개 (오래된 순):"] + tail([line(n) for n in top], 30)
    for title, test in (("열린 todo:", is_open), ("보류된 todo:", is_held)):
        todo = [n for n in st["nodes"] if test(n)]
        if todo:
            out += [title] + [f"{line(n)}  ← {under(n)['name']}" for n in todo]
    out += [f"마지막 항목 '{top[-1]['name']}' 의 하위:"] + tail(lines(st, top[-1]["id"], 1), 40)
    return "\n".join(out)


def cache_line(root):
    """상태 안내·doctor 에 붙일 cache 한 줄."""
    c, _ = load_cache(root)
    if c is None:
        return "없음 (이 PC 에서 sync 한 적 없음 — 시작할 때 state 들을 모두 합친다)"
    return f"{cache_age(c)}, 노드 {len(c['nodes'])}개"


def summary(st, sid=None):
    f = focus(st)
    return (f"기록 root: '{st.get('root_name')}' {wfapi.url(st['root'])}  (root id: {short(st['root'])})\n"
            f"지금 도구 실행이 붙는 노드: {f['name'] + ' (id: ' + short(f['id']) + ')' if f else '없음'}"
            + (sync_status(sid) if sid else "") + f"\ncache: {cache_line(st['root'])}"
            + f"\n지금까지 쓴 노드:\n{outline(st)}")

# ----------------------------------------------------------------- /workflowy:workstream


REMIND = ("[workflowy] 이 세션은 Workflowy 에 기록 중이다. 이 요청도 진행하는 대로 root 아래에 정리해 쓴다 "
          "(새 요청은 root 아래에 request: true, 단계는 todo, 단계의 발견·결과는 그 todo 아래에 쓰고 close. "
          "하지 않은 todo 는 done 으로 닫지 않는다). "
          "도구 description 은 사용자의 언어로, 명사형으로 짧게 쓴다 — 그대로 기록된다.")


def clear_cache(st, sid):
    """clear-cache: 이 root 의 cache 를 비우고, 이 세션이 이어받은 노드도 요청과 이 세션이 쓴 노드의 조상만 남긴다.
    root 등록과 이 세션이 쓴 노드, Workflowy 는 건드리지 않는다. 옛 cache 에서 이어 오던 값(steps, 못 읽은 하위)을
    끊는 유일한 길이다. 파일을 지우지 않고 비운 시각을 남긴다 — cache 가 없으면 state 들을 모두 합치는
    3.4 방식(from_states)으로 돌아가 낡은 사본을 다시 불러오기 때문이다."""
    job = load_job(sid)
    if job_state(job) == "running":              # 끝나면 옛 cache 로 채운 트리를 cache 에 쓰게 된다
        return (f"[workflowy] sync 가 돌고 있어 cache 를 비우지 않았다 ({progress_line(job)}). "
                "끝난 뒤 다시 부르라고 사용자에게 알린다.")
    root = st["root"]
    try:
        save_cache(root, {"root": root, "since": time.time(), "by": "clear-cache", "nodes": []})
    except OSError as e:
        return f"[workflowy] cache 를 비우지 못했다 ({e}). 다시 부르라고 사용자에게 알린다."
    say = [x for x in (sync_news(sid),) if x]    # 앞 sync 의 결과가 남아 있으면 먼저 전한다
    top, byid, keep = short(root), {short(n["id"]): n for n in st["nodes"]}, set()
    for n in st["nodes"]:
        p = None if n.get("old") else n["parent"]
        while p in byid and p not in keep:
            keep.add(p)
            p = byid[p]["parent"]
    before = len(st["nodes"])
    st["nodes"] = [n for n in st["nodes"] if not n.get("old") or n["parent"] == top or short(n["id"]) in keep]
    save(sid, st)
    return "\n".join(say + [
        f"[workflowy] cache 를 비웠다. 이 세션이 이어받은 노드도 요청과, 이 세션이 쓴 노드의 조상만 남기고 "
        f"{before - len(st['nodes'])}개를 뺐다. 앞서 받은 트리에서 빠진 노드는 이제 parent 로 쓸 수 없고, 판단의 근거로 삼지 않는다. "
        "이 세션이 쓴 노드와 root 등록, Workflowy 는 그대로다. 요청의 하위가 필요하면 사용자에게 "
        "/workflowy:workstream sync 를 권한다 (옛 cache 없이 Workflowy 에서 새로 받는다).\n지금 트리:\n" + outline(st)])


def h_prompt(ev, st, sid, arg):
    if arg is None:                              # 사용자 요청: 기록 중이면 지침을 짧게 상기시킨다. 끝난 sync 결과도 전한다
        by_user = ev.get("source", "user") == "user" and not SYSTEM.match(ev.get("prompt") or "")
        say = [x for x in (sync_news(sid), REMIND if st.get("root") and by_user else None) if x]
        return "\n".join(say) or None
    a = arg.strip()
    if a in ("", "status"):
        if not st.get("root"):
            return "[workflowy] 기록 중인 세션이 없습니다."
        return "\n".join(x for x in ("[workflowy] " + summary(st, sid), sync_news(sid)) if x)
    if a == "stop":
        if not st.get("root"):
            return "[workflowy] 기록 중인 세션이 없습니다."
        archive(sid, st)
        return ("[workflowy] 기록을 멈췄습니다. 이미 쓴 노드는 그대로 두고, 다음에 같은 노드로 시작하면 이어받습니다. "
                "이 세션에서는 더 이상 workflowy 도구를 쓰지 않는다.")
    if a in ("sync", "clear-cache"):
        if not st.get("root"):
            return "[workflowy] 기록 중인 세션이 없습니다. /workflowy:workstream <노드 id> 로 먼저 시작하세요."
        return start_sync(st, sid) if a == "sync" else clear_cache(st, sid)
    if a == "doctor":
        buf = io.StringIO()
        with redirect_stdout(buf):
            do_doctor(st)
        return "[workflowy] 점검 결과\n" + buf.getvalue()
    s = short(a.split()[0])
    if not s:
        return (f"[workflowy] '{a}' 는 노드 id 가 아닙니다. Workflowy URL 끝 12자리나 URL 을 주세요 "
                "(예: /workflowy:workstream daa0961ddeee).")
    if st.get("root") and short(st["root"]) == s:
        return "[workflowy] 이미 이 노드에 기록 중입니다.\n" + summary(st)
    try:
        n = wfapi.get(s)
    except urllib.error.HTTPError as e:
        return "[workflowy] 기록을 시작하지 못했습니다: " + {
            401: "API key 가 잘못됨", 403: "권한 없음", 404: f"노드 {s} 를 찾을 수 없음"}.get(e.code, f"HTTP {e.code}")
    prev = st.get("root_name")
    if st.get("root"):
        archive(sid, st)
    cached, c, cwarn = from_cache(sid, n["id"])
    old, warn = read(n["id"], cached, full=False)
    fell = old is None
    if fell:
        old, warn = cached, warn + " cache 로만 이어받았다 (요청 목록도 cache 의 것이다). 다른 PC 의 기록과 Workflowy 에서 고친 내용은 빠져 있다."
    warn = " ".join(x for x in (warn, cwarn) if x)
    st.clear()
    st.update(root=n["id"], root_name=norm(n.get("name"))[:80] or "(제목 없음)",
              started=time.time(), cwd=ev.get("cwd"), nodes=old)
    save(sid, st)
    out = (f"[workflowy] 기록 시작: '{st['root_name']}' {wfapi.url(n['id'])}\n"
           f"root id: {s} — 이 노드 자체는 건드리지 않고, 그 아래에 workflowy create 도구로 쓴다. "
           "root 바로 아래에는 요청만 쓴다: 새 요청은 create(parent=root, request: true).")
    if prev:
        out += f"\n(이전에 기록하던 '{prev}' 대신 이 노드에 기록한다. 이전 노드들은 parent 로 쓸 수 없다.)"
    if old:
        ch = kids(st)
        bare = sum(1 for x in old if x.get("request") and not ch.get(short(x["id"])))
        src = (f"이 PC 의 cache({cache_age(c)})에 그 뒤 이 PC 의 세션들이 쓴 것을 더한 것이다" if c else
               "이 PC 에 이 root 의 cache 가 아직 없어(sync 한 적 없음) 이 PC 의 state 들을 모두 합친 것이다 — "
               "끝난 세션의 낡은 사본이 섞여 있을 수 있다")
        out += (f"\n이 노드 아래에 이미 있는 노드 {len(old)}개를 이어받았다. "
                + ("" if fell else "요청 목록은 Workflowy 에서 읽었고, 요청의 하위는 " + src
                   + (f" (요청 {bare}개는 하위가 없어 제목만 보인다)" if bare else "") + ". "
                   "그 뒤 다른 PC 에서 쓰거나 Workflowy 에서 직접 고친 내용까지 봐야 하면 사용자에게 /workflowy:workstream sync 를 권한다. ")
                + "먼저 아래 내용으로 지금까지의 흐름을 파악한다. 이어받은 노드 아래에도 쓸 수 있고, 이어받은 todo 도 close 할 수 있다.\n"
                + outline(st) + pending(old))
    if warn:
        out += "\n" + warn
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
        return decide(False, "이 세션은 Workflowy 에 기록 중이 아니다. 사용자가 /workflowy:workstream <id> 로 시작해야 쓸 수 있다.")
    i, tool = ev.get("tool_input") or {}, ev.get("tool_name", "")[len(TOOL):]
    if tool == "create":
        return decide(*check_create(st, i))
    if tool == "close":
        return decide(*check_close(st, i))
    return decide(False, f"알 수 없는 workflowy 도구: {tool}")


def check_create(st, i):
    """(허용 여부, 거부 이유). root 바로 아래에는 request=true 로 밝힌 요청만 들어간다."""
    p, root, req = short(i.get("parent")), short(st["root"]), i.get("request") is True
    if not (p and (p == root or find(st, p))):
        return False, (f"parent 는 기록 root({root}) 이거나 이 세션에서 만들었거나 이어받은 노드여야 한다. "
                       "지금까지 쓴 노드:\n" + tree(st, 30))
    if p == root and not req:
        r = last_request(st)
        return False, ("root 바로 아래에는 요청만 쓴다. 새 요청이면 request: true 로 만들고, "
                       "요청 안에 쓸 내용이면 parent 를 그 요청(이나 하위 노드)으로 준다."
                       + (f" 지금 요청: '{r['name']}' (id: {short(r['id'])})" if r else ""))
    if req and p != root:
        return False, f"요청(request: true)은 root({root}) 바로 아래에만 만든다. 요청 안의 주제는 bullets 로 쓴다."
    if req and (i.get("type") or "bullets") != "bullets":
        return False, "요청은 bullets 로 만든다 (굵게와 날짜·시각 note 는 서버가 붙인다)."
    return True, ""


def check_close(st, i):
    """(허용 여부, 거부 이유). 하지 않은 todo 가 결과 없이 완료로 남지 않게 한다."""
    ids, outcome = wfapi.ids_of(i.get("ids")), i.get("outcome") or "done"
    reason = str(i.get("reason") or "").strip()
    if not ids:
        return False, "ids 에 닫을 todo 의 id 를 준다."
    if outcome not in wfapi.OUTCOMES:
        return False, f"outcome 은 {' | '.join(wfapi.OUTCOMES)} 중 하나다."
    if outcome != "done" and not reason:
        return False, f"{outcome} 은 reason 이 필요하다 (todo 아래에 '{wfapi.close_note(outcome, '…')}' 로 쓰인다)."
    ns = []
    for x in ids:
        n = find(st, x)
        if not n:
            return False, f"{x}: 이 세션에서 만들었거나 이어받은 todo 만 닫을 수 있다 (root 와 그 밖의 노드는 건드리지 않는다)."
        if n["type"] != "todo":
            return False, f"'{n['name']}' 은 todo 가 아니다 ({n['type']})."
        if n.get("done"):
            return False, f"'{n['name']}' 은 이미 닫혔다."
        if outcome == "hold" and n.get("held"):
            return False, f"'{n['name']}' 은 이미 보류되었다. 이어서 하려면 그 아래에 새 todo 를 만든다."
        ns.append(n)
    ch, closing = kids(st), {short(n["id"]) for n in ns}
    for n in ns:
        # close 가 쓴 이유 노드(⏸ 보류 등)는 결과가 아니다
        if outcome == "done" and not reason and not [c for c in ch.get(short(n["id"]), []) if not c.get("by")]:
            return False, (f"'{n['name']}' 아래에 쓴 결과가 없다. 끝냈으면 reason 에 한 줄 결과를 넣는다. "
                           "하지 않았다면 outcome 을 cancel(하지 않기로 함)·replace(방법이 바뀜)·hold(나중에 함) 중에서 고른다.")
        # 하위 todo 가 열린 채 남으면 도구 실행 대상에서 빠지거나(닫힌 부모 아래) 보류한 일 아래에서 이어진다
        left = [d for d in below(ch, n) if d["type"] == "todo" and not d.get("done") and d.get("steps", True)
                and short(d["id"]) not in closing and not (outcome == "hold" and d.get("held"))]
        if left:
            return False, (f"'{n['name']}' 아래에 아직 닫지 않은 todo 가 있다: "
                           + ", ".join(f"'{d['name']}' ({short(d['id'])})" for d in left) + ". ids 에 함께 넣는다.")
    return True, ""


ID     = re.compile(r"id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
# close 의 결과 줄 (mcp.py close). 응답이 JSON 으로 감싸여 와도 읽히도록 id 는 16진수와 - 만 받는다.
NOTE   = re.compile(r"note: ([0-9a-f-]{12,36}) under ([0-9a-f-]{12,36})")
CLOSED = re.compile(r"closed: ([0-9a-f-]{12,36}) (" + "|".join(wfapi.OUTCOMES) + r")\b")


def h_track(ev, st, sid):
    if ev.get("agent_id") or not st.get("root"):
        return
    i, tool = ev.get("tool_input") or {}, ev.get("tool_name", "")[len(TOOL):]
    r = ev.get("tool_response")
    r = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    if tool == "create":
        m = ID.search(r)
        if not m:
            return                               # 실패한 호출
        t, req = i.get("type") or "bullets", i.get("request") is True
        name = norm(str(i.get("name") or "").strip("\n").split("\n")[0]) if t != "code" else "(코드)"
        name = wfapi.request_title(name) if req else name      # 서버가 걷어 내는 ** 까지 sync 로 읽은 제목과 같게
        st["nodes"].append({"id": m.group(1), "parent": short(i.get("parent")), "type": t, "t": time.time(),
                            "name": clip(name), **({"steps": bool(i["steps"])} if "steps" in i else {}),
                            **({"request": True, "at": f"{datetime.now():%Y-%m-%d %H:%M}"} if req else {})})
    elif tool == "close":
        outcome = i.get("outcome") or "done"
        name = clip(norm(wfapi.close_note(outcome, str(i.get("reason") or "").strip()).split("\n")[0]))
        for nid, parent in NOTE.findall(r):         # close 가 쓴 이유 노드. by 로 결과와 구별한다
            st["nodes"].append({"id": nid, "parent": short(parent), "type": "bullets", "t": time.time(),
                                "name": name, "by": outcome})
        for cid, o in CLOSED.findall(r):         # ct: 닫은 시각. 다른 세션이 cache 를 받은 뒤에 닫았는지 가린다
            n = find(st, cid)
            if n and o == "hold":
                n.update(held=True, ct=time.time())
            elif n:
                n.update(done=True, outcome=o, ct=time.time())
                n.pop("held", None)
    save(sid, st)


def clip(s, n=60): return s[:n] + ("…" if len(s) > n else "")

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
    return ("[workflowy] 이 세션은 Workflowy 에 작업 기록 중이다 (/workflowy:workstream 스킬의 지침을 계속 따른다).\n"
            "- 지금 하는 작업은 workflowy create 도구로 root 아래에 정리해 쓰고 (새 요청은 request: true), "
            "todo 는 끝나는 대로 close 한다 "
            "(끝냄 done · 안 함 cancel · 방법 바뀜 replace · 미룸 hold).\n"
            "- 도구 실행은 훅이 열린 todo 아래에 자동으로 붙인다. 도구의 description 은 명사형으로 짧게 쓴다.\n"
            "- 이미 쓴 노드는 고치거나 지우지 않고, 새 노드를 추가만 한다.\n" + summary(st, ev.get("session_id"))
            + ("\n" + (sync_news(ev.get("session_id")) or "")).rstrip())

# ----------------------------------------------------------------- 점검


def new_errors():
    """지난 확인 이후 쌓인 오류 줄. 기록 실패가 조용히 묻히지 않게 한 번씩 알린다."""
    seen = DATA_DIR / "error.seen"
    try:
        size = ERRLOG.stat().st_size
        done = int(seen.read_text()) if seen.exists() else 0
        if size < done:                          # 로그를 비웠거나 줄였으면 처음부터 읽는다
            done = 0
        with ERRLOG.open("rb") as f:
            f.seek(done)
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
        seen.write_text(str(size))
        return lines
    except OSError:
        return []


def do_doctor(st):
    ok = True
    key = wfapi.conf("WORKFLOWY_API_KEY", "api_key")
    print(f"  {'ok ' if key else 'FAIL'} API key      {'설정됨' if key else '없음'}")
    print(f"  ok  데이터 폴더  {DATA_DIR}")
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
    if key and ok and st.get("root"):
        try:
            check_tree(st["root"])
        except Exception as e:
            print(f"  FAIL 트리 읽기    {type(e).__name__}: {e}"); ok = False
    old = sum(1 for n in st.get("nodes") or [] if n.get("old"))
    print(f"  --  state        " + (f"'{st['root_name']}' 에 기록 중, 만든 노드 {len(st['nodes']) - old}개"
                                  + (f", 이어받은 노드 {old}개" if old else "") if st.get("root") else "기록 중 아님"))
    if st.get("root"):
        print(f"  --  cache        {cache_line(st['root'])}  ({cpath(st['root'])})")

    if ERRLOG.exists() and ERRLOG.stat().st_size:
        print(f"\n  주의: {ERRLOG} 에 기록된 오류가 있습니다 (마지막 5줄)")
        for ln in ERRLOG.read_text(encoding="utf-8").strip().split("\n")[-5:]:
            print("        " + ln)

    print("\n" + ("전부 정상." if ok else "문제가 있습니다. 위의 FAIL 항목을 확인하세요."))
    return 0 if ok else 1


def check_tree(root, limit=LIMIT):
    """이어받기가 root 아래를 얼마나 빨리, 어디까지 읽는지. API 가 돌려주는 이름·layoutMode 원문도 보여 준다."""
    try:
        t, via = wfapi.subtree(short(root), limit), "short id"
    except urllib.error.HTTPError as e:
        print(f"  --  트리 읽기    short id 로 실패 (HTTP {e.code}), 전체 UUID 로 다시 읽음")
        t, via = wfapi.subtree(root, limit), "UUID"
    ns, times = t["nodes"], sorted(t["times"])
    top = [n for n in ns if short(n.get("parent_id")) == short(root)]
    tools = [n for n in ns if wfapi.tool_run(n)]
    kinds = {}
    for n in ns:
        k = (n.get("data") or {}).get("layoutMode") or "(없음)"
        kinds[k] = kinds.get(k, 0) + 1
    retried = ", ".join(f"HTTP {c} {k}번" for c, k in sorted(wfapi.RETRIED.items())) or "없음"
    print(f"  {'ok ' if not t['missing'] else 'WARN'} 트리 읽기    노드 {len(ns)}개 (요청 {len(top)}개, ▹ 도구 실행 {len(tools)}개), "
          f"호출 {len(times) + t['errors'] + 1}번, {t['seconds']:.1f}초 (한도 {limit}초, {via})")
    if times:
        print(f"                   호출당 중앙값 {times[len(times) // 2]:.2f}초, 최대 {times[-1]:.2f}초")
    print(f"                   실패 {t['errors']}번, 재시도 {retried}, 못 읽은 노드 {len(t['missing'])}개")
    print("  --  layoutMode   " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))

    def raw(s): return json.dumps(str(s or "")[:80], ensure_ascii=False)
    def has(n, *xs): return any(x in (n.get("name") or "") for x in xs)
    def code(n): return (n.get("data") or {}).get("layoutMode") == "code-block"
    for label, n, f in (
            ("요청 제목", top[-1] if top else None, "name"),
            ("요청 note", top[-1] if top else None, "note"),
            ("코드 블록", next((n for n in ns if code(n)), None), "name"),
            ("인라인 코드", next((n for n in ns if has(n, "`", "<code>") and not code(n)), None), "name"),
            ("▹ 기록", tools[0] if tools else None, "name")):
        if n:
            print(f"  --  원문         {label}: {raw(n.get(f))}")

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
    if not DATA_DIR:
        print("[workflowy] 플러그인 훅 밖에서 실행되어 API key 와 데이터 폴더(state·cache)를 쓸 수 없습니다.\n"
              "점검은 /workflowy:workstream doctor 로 하세요.", file=sys.stderr)
        sys.exit(1)
    if mode == "sync-run":                       # 훅이 아니라 start_sync 가 띄운 백그라운드 프로세스
        try: run_sync(sys.argv[2])
        except Exception as e: log_error("sync", e)
        sys.exit(0)
    try: ev = json.loads(sys.stdin.read() or "{}")
    except Exception: ev = {}
    sid = ev.get("session_id", "")

    # 모든 요청·도구마다 불리는 훅은 할 일이 없으면 잠금도 잡지 않고 끝낸다
    m = SKILL.match((ev.get("prompt") or "").strip()) if mode == "prompt" else None
    d = step_of(ev) if mode == "step" else None
    if (mode == "step" and not d) or \
       ((mode in ("step", "session-start") or (mode == "prompt" and not m)) and not spath(sid).exists()):
        sys.exit(0)

    say, target, ctx = None, None, None
    lk = lock(sid)
    try:
        st = load(sid)
        if   mode == "prompt":        say = h_prompt(ev, st, sid, m.group(1) if m else None)
        elif mode == "guard":         h_guard(ev, st)
        elif mode == "track":
            h_track(ev, st, sid)
            ctx = None if ev.get("agent_id") else sync_news(sid)     # 끝난 sync 결과는 도구 결과 뒤에 붙여 전한다
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
        try: wfapi.create(target, wfapi.TOOL + wfapi.label(d, 200))
        except Exception as e: log_error(mode, e)
    if say:                                      # UserPromptSubmit·SessionStart 의 stdout 은 Claude 의 컨텍스트가 된다
        print(say)
    if ctx:                                      # PostToolUse 는 JSON 의 additionalContext 로 전한다
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ctx}},
                         ensure_ascii=False))
    sys.exit(0)   # 훅은 무슨 일이 있어도 0. exit 2는 세션을 차단한다.


if __name__ == "__main__":
    main()
