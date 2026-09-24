#!/usr/bin/env python3
"""
look_tree.py - /workflowy:look-tree <id>: Workflowy 노드 하나와 그 하위 전체를 읽어 scratchpad 에 텍스트 파일로 저장한다.
읽기만 한다. sync 처럼 훅은 백그라운드 프로세스를 띄우고 곧바로 끝난다 (훅 timeout 과 무관하게 끝까지 읽는다).

  (인자 없음)  UserPromptSubmit 훅: /workflowy:look-tree <id> 면 run 을 띄운다. 끝났는데 전하지 않은 결과가 있으면 전한다
  run <job>    백그라운드: <id> 와 하위 전체를 읽어 저장하고 결과를 작업 파일에 적는다.
               API key 는 훅에서 물려받는다 — Claude 의 Bash 에는 key 가 없어 Claude 는 읽기를 시작할 수 없다
  wait <job>   Claude 가 Bash 백그라운드로 실행한다: 작업이 끝날 때까지 기다렸다 결과를 출력한다 (Workflowy 를 부르지 않는다)

작업 파일은 데이터 폴더의 look-tree/<session id>-<short id>.json 이다. 결과를 전하면 지운다.
노드는 Workflowy API 가 준 값만으로 적는다. 읽는 트리에 무엇이 있을지 모르므로 workstream 의 규칙
(wf.py 의 from_api·line 등)은 쓰지 않는다. wf 에서는 파일·프로세스 도우미(put·job_state)만 가져다 쓴다.
"""
import json, os, pathlib, re, subprocess, sys, tempfile, time, urllib.error
from datetime import datetime
import wfapi
from wfapi import short
from wf import DATA_DIR, put, job_state

CMD    = re.compile(r"^/(?:workflowy:)?look-tree(?![\w-])\s*(.*)$", re.S)    # 사용자가 직접 친 스킬 (접두사 없이도)
REASON = {401: "API key 가 잘못됨", 403: "권한 없음", 404: "노드를 찾을 수 없음"}


def jpath(sid, s): return DATA_DIR / "look-tree" / f"{sid}-{s}.json"


def jobs(sid):
    return sorted((DATA_DIR / "look-tree").glob(f"{sid}-*.json")) if DATA_DIR else []


def load(job):
    try:    return json.loads(pathlib.Path(job).read_text(encoding="utf-8"))
    except Exception: return {}

# ----------------------------------------------------------------- 훅


def hook(ev):
    sid = ev.get("session_id", "")
    say = [x for x in map(deliver, jobs(sid)) if x]          # 끝났는데 전하지 않은 결과 (다음 메시지로 전하는 길)
    m = CMD.match((ev.get("prompt") or "").strip())
    if m and ev.get("source", "user") == "user":             # 사용자가 직접 친 명령만. Claude 는 부를 수 없다
        say.append(start(sid, m.group(1), ev.get("scratchpad_dir")))
    if say:
        print("\n".join(say))                                # UserPromptSubmit 의 stdout 은 Claude 의 context 가 된다


def start(sid, arg, folder):
    s = short((arg.split() or [""])[0])
    if not s:
        return ("[workflowy] look-tree: 노드 id 가 없거나 형식이 아니다. /workflowy:look-tree <노드 id 또는 URL> 로 부른다 "
                "(URL 끝 12자리, URL, 전체 UUID).")
    if not DATA_DIR:
        return "[workflowy] look-tree: 데이터 폴더를 몰라 시작하지 못했다 (플러그인을 다시 설치하거나 /workflowy:workstream doctor 로 점검한다)."
    job = jpath(sid, s)
    j = load(job)
    if job_state(j) == "running":
        return (f"[workflowy] look-tree: 노드 {s} 를 이미 읽는 중이다 ({int(time.time() - j.get('started', time.time()))}초째). "
                "끝나면 알린다.\n" + how_to_wait(job))
    # scratchpad 가 없는 세션(훅 입력에 scratchpad_dir 가 없다)은 OS 임시 폴더 아래 세션마다 따로 둔다
    base = pathlib.Path(folder) if folder else pathlib.Path(tempfile.gettempdir()) / "workflowy-look-tree" / (sid or "session")
    out = base / f"workflowy-{s}.md"
    try:
        out.unlink(missing_ok=True)                          # 끝나기 전에 옛 파일을 새 결과로 알고 읽지 않게
        put(job, {"status": "running", "id": s, "out": str(out), "started": time.time(), "pid": None})
        spawn(job)
    except Exception as e:
        job.unlink(missing_ok=True)
        return f"[workflowy] look-tree 을 시작하지 못했다 ({type(e).__name__}: {e})."
    return (f"[workflowy] look-tree 시작: 노드 {s} 와 그 하위 전체를 백그라운드에서 읽는다. "
            f"끝나면 {out} 에 저장된다 (그 전에는 파일이 없다).\n" + how_to_wait(job) + "\n"
            "기다리지 않으면 결과는 사용자의 다음 메시지 뒤에 온다.")


def how_to_wait(job):
    me = pathlib.Path(os.path.abspath(__file__)).as_posix()
    return f'기다리려면 Bash 를 run_in_background 로 실행한다: python3 "{me}" wait "{pathlib.Path(job).as_posix()}"'


def spawn(job):
    """run 을 분리된 프로세스로 띄운다 (wf.launch 와 같은 방식). 표준 입출력을 끊어야 Claude Code 가 파이프를 기다리지 않는다.
    API key 와 CLAUDE_PLUGIN_DATA 는 환경변수로 그대로 넘어간다 (Claude 에게는 보이지 않는다)."""
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", str(job)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)


def deliver(job):
    """끝난 작업의 결과 글. 작업 파일을 먼저 지운 쪽만 돌려준다 — wait 와 다음 메시지가 겹쳐도 한 번만 전한다."""
    j = load(job)
    if not j or job_state(j) == "running":
        return None
    try:
        pathlib.Path(job).unlink()
    except OSError:                                          # 이미 지웠거나(다른 쪽이 전함) 다른 프로세스가 여는 중
        return None
    return j.get("message") or (f"[workflowy] look-tree: 노드 {j.get('id')} 를 읽던 백그라운드 프로세스가 결과 없이 멈췄다 "
                                "(중단됨). 파일은 만들지 않았다. /workflowy:look-tree 로 다시 부른다.")

# ----------------------------------------------------------------- 백그라운드 읽기


def run(job):
    """<id> 노드와 하위 전체를 끝까지 읽어(sync 와 같은 읽기: 병렬, 429 는 모두 멈춰 기다림) 파일로 저장한다.
    파일은 다 쓴 뒤에 바꿔 넣는다 — 파일이 있으면 완성본이다. 결과 글은 작업 파일에 남겨 wait 나 다음 훅이 전한다."""
    t0 = time.time()
    j = load(job)
    j["pid"] = os.getpid()
    put(job, j)
    s, out = j["id"], pathlib.Path(j["out"])
    try:
        top = wfapi.get(s)
        t = wfapi.subtree(top["id"], None, skip=None)       # 끝까지. ▹ 도 보통 노드로 보고 모든 노드의 자식을 읽는다
        lines, count = render(top, t)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, out)
        j.update(status="done", message=summary(out, top, lines, count, t, time.time() - t0))
    except urllib.error.HTTPError as e:
        j.update(status="failed", message=f"[workflowy] look-tree 실패: 노드 {s} — {REASON.get(e.code, e.reason or '')} "
                                          f"(HTTP {e.code}). 파일은 만들지 않았다.")
    except Exception as e:
        j.update(status="failed", message=f"[workflowy] look-tree 실패: 노드 {s} — {type(e).__name__}: {e}. 파일은 만들지 않았다.")
    j["ended"] = time.time()
    put(job, j)


def render(top, t):
    """(파일의 줄들, 노드 수). 머리 한 줄 뒤에 <id> 노드부터 트리 순서로 노드마다 node_lines.
    자식은 children() 이 priority 순으로 준 차례 그대로다."""
    ch = {}
    for n in t["nodes"]:
        ch.setdefault(short(n.get("parent_id")), []).append(n)
    cut, body, seen = {short(x) for x in t["missing"]}, [], set()

    def walk(n, d):
        k = short(n["id"])
        if k in seen:                                        # 같은 노드가 또 오면(미러 등) 한 번만 적는다
            return
        seen.add(k)
        body.extend(node_lines(n, d, k in cut))
        for c in ch.get(k, []):
            walk(c, d + 1)

    walk(top, 0)
    head = f"# {wfapi.url(top['id'])} · {datetime.now():%Y-%m-%d %H:%M} 에 Workflowy 에서 읽음 · 노드 {len(seen)}개"
    return [head] + body, len(seen)


def node_lines(n, d, cut):
    """API 가 준 값만으로 노드 하나를 줄로: 완료(✓), layoutMode 원문, 제목, short id,
    그 아래 제목의 나머지 줄(┆)과 note(│). 제목·note 는 HTML 을 평문으로만 바꾼다(unhtml)."""
    pad, mode = "  " * d, (n.get("data") or {}).get("layoutMode")
    first, *rest = text(n.get("name")).split("\n")
    head = (f"{pad}- {'✓ ' if n.get('completed') else ''}{f'[{mode}] ' if mode else ''}{first}"
            f"  (id: {short(n['id'])})" + ("  [하위 못 읽음]" if cut else ""))
    note = text(n.get("note")).split("\n") if n.get("note") else []
    return [head] + [f"{pad}  ┆ {x}" for x in rest] + [f"{pad}  │ {x}" for x in note]


def text(s): return wfapi.unhtml(s).replace("\r\n", "\n")


def summary(out, top, lines, count, t, sec):
    title = " ".join(text(top.get("name")).split())
    title = title[:60] + ("…" if len(title) > 60 else "")
    calls = len(t["times"]) + t["errors"] + 2                # + 노드 자체(get) 와 그 자식(root 의 children)
    say = [f"[workflowy] look-tree 끝남: {out}",
           f"'{title}' {wfapi.url(top['id'])} — 노드 {count}개(이 노드 포함), {len(lines)}줄, "
           f"{out.stat().st_size / 1024:.0f}KB",
           f"읽기 {sec:.0f}초: 호출 {calls}번" + (f", 한도(429) 대기 {t['waits']}번" if t["waits"] else "")]
    if t["missing"]:
        why = []
        if t["errors"]:
            why.append(f"호출 실패 {t['errors']}번: " + ", ".join(
                f"{k} {v}번" for k, v in sorted(t["reasons"].items(), key=lambda x: -x[1])))
        if len(t["missing"]) > t["errors"]:
            why.append(f"결과 없이 멈춰 못 읽음 {len(t['missing']) - t['errors']}개")
        say.append(f"주의: {len(t['missing'])}개 노드의 하위를 읽지 못함 ({'; '.join(why)}). "
                   "파일에서 '[하위 못 읽음]' 으로 찾는다.")
    return "\n".join(say)

# ----------------------------------------------------------------- 기다리기 (Claude 가 Bash 로)


def wait(job):
    """작업이 끝날 때까지 기다렸다 결과 글을 출력한다. Workflowy 를 부르지 않는다 (API key 도 필요 없다)."""
    while True:
        j = load(job)
        if not j:
            print("[workflowy] look-tree: 결과를 이미 전했다 (또는 없는 작업이다).")
            return
        if job_state(j) != "running":
            break
        time.sleep(1)
    print(deliver(job) or "[workflowy] look-tree: 결과를 이미 전했다.")

# ----------------------------------------------------------------- 진입점


def main():
    # Windows 에서 파이프로 연결된 표준 입출력은 로캘 인코딩(cp949 등)을 쓴다. UTF-8 로 맞춘다
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode in ("run", "wait") and len(sys.argv) > 2:
        (run if mode == "run" else wait)(pathlib.Path(sys.argv[2]))
        return
    try: ev = json.loads(sys.stdin.read() or "{}")
    except ValueError: ev = {}
    try:
        hook(ev)
    except Exception as e:
        if CMD.match((ev.get("prompt") or "").strip()):        # 다른 프롬프트에는 아무것도 붙이지 않는다
            print(f"[workflowy] look-tree 실패: {type(e).__name__}: {e}")
    sys.exit(0)   # 훅은 무슨 일이 있어도 0. exit 2는 프롬프트를 막는다.


if __name__ == "__main__":
    main()
