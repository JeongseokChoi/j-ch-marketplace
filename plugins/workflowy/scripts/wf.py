#!/usr/bin/env python3
"""
wf.py - Claude Code 세션을 Workflowy에 실시간 기록한다. workflowy 플러그인의 훅이 부른다.

API key 와 상태 저장 위치는 플러그인이 훅 프로세스에만 넘겨준다(CLAUDE_PLUGIN_OPTION_*,
CLAUDE_PLUGIN_DATA). Claude 가 Bash 도구로 실행하면 둘 다 없어서 기록할 수 없다.
그래서 session-log 스킬도 Claude 가 명령을 실행하는 대신 훅이 스킬 호출을 받아 처리한다.
"""
import html, io, json, os, re, shutil, subprocess, sys, time, pathlib, urllib.request, urllib.error
from contextlib import redirect_stdout
from datetime import datetime

API    = "https://workflowy.com/api/v1"
DATA   = os.environ.get("CLAUDE_PLUGIN_DATA")
STATE  = pathlib.Path(DATA) if DATA else None       # 업데이트에도 보존되는 플러그인 데이터 영역
ERRLOG = STATE / "error.log" if STATE else None
SKILL  = re.compile(r"^/(?:workflowy:)?session-log\b\s*(.*)$", re.S)   # 사용자가 직접 입력한 스킬

SECRET = re.compile(
    r"sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}|(?i:bearer)\s+[A-Za-z0-9._\-]{20,}"
    r"|(?i:api[_\-]?key|token|password|secret)\s*[=:]\s*\S{8,}")

# name 필드는 마크다운을 파싱한다. 백슬래시 이스케이프가 통하는 문자들.
MD = re.compile(r"([*`\[\]])")

# ----------------------------------------------------------------- 공통


def conf(env, key):
    """우선순위: 환경변수 -> 플러그인 userConfig (CLAUDE_PLUGIN_OPTION_<KEY>)."""
    v = os.environ.get(env)
    if v:
        return v.strip()
    want = "claude_plugin_option_" + key             # 문서는 대문자로 쓰지만 대소문자를 가리지 않는다
    for k, val in os.environ.items():
        if k.lower() == want and val.strip():
            return val.strip()
    return None


def scrub(s, n=400, lines=False):
    """비밀값을 가리고 길이를 자른다. 제목은 한 줄이어야 하고, 노트(lines=True)는 줄바꿈을 살린다."""
    s = SECRET.sub("<<redacted>>", str(s or "")).replace("\r\n", "\n")
    if not lines:
        s = s.replace("\n", " / ")
    return (s[:n] + "…") if len(s) > n else s


def plain(s):
    """노트용: Workflowy 노트는 마크다운을 그리지 않으므로 기호만 걷어낸다."""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)            # [x](url) -> x
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.M)               # 제목 기호
    return re.sub(r"\*\*|__|`", "", s)


def label(s, n=400):
    """create용: name 필드의 마크다운 파싱을 중화한다."""
    return MD.sub(r"\\\1", scrub(s, n)).replace("~~", "∼∼")


def html_label(s, n=400):
    """update용: POST /nodes/:id 는 마크다운이 아니라 HTML만 해석한다."""
    return html.escape(scrub(s, n), quote=False)


def call(method, path, body=None, tries=3):
    key = conf("WORKFLOWY_API_KEY", "api_key")
    if not key:
        raise RuntimeError("Workflowy API key 없음 (플러그인 설정의 api_key 를 확인하세요)")
    data = json.dumps(body).encode() if body is not None else None
    for i in range(tries):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(1.5 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(1.0); continue
            raise


# 턴·에이전트·세션 노드는 todo 로 만들지 않고 완료 처리도 하지 않는다. 완료된 항목은 취소선에
# 흐려져 읽기 나쁘고, 언제 완료할지 정확히 알 신호(취소 훅 등)도 없다. 상태는 제목으로만 나타낸다:
# 진행 중은 시각만(턴) 또는 "· 진행 중"(에이전트), 끝나면 요약과 소요 시간, 안 끝나면 취소됨/중단됨.


def node(parent, name, note=None, pos="bottom"):
    b = {"parent_id": parent, "name": name, "position": pos}
    if note:   b["note"] = note[:8000]
    return call("POST", "/nodes", b)["item_id"]


def edit(nid, **kw): call("POST", f"/nodes/{nid}", kw)
def short(nid):      return nid.split("-")[-1]

def spath(sid): return STATE / "state" / f"{sid}.json"

def load(sid):
    try:    return json.loads(spath(sid).read_text(encoding="utf-8"))
    except Exception: return {}

def save(sid, st):
    p = spath(sid); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st), encoding="utf-8")


def lock(sid):
    """훅이 동시에 돌 때(병렬 서브에이전트 등) 상태 파일을 지킨다. 못 잡으면 None."""
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


def texts(content):
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else ""


def when(r):
    try:    return datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
    except Exception: return None


TASK_DONE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>.*?<status>([^<]+)</status>", re.S)
LAUNCHED  = re.compile(r"Async agent launched.*?agentId: ([0-9a-f]+)", re.S)
HANDBACK  = re.compile(r'<agent-message from="([0-9a-f]+)">(.*?)(?:</agent-message>|$)', re.S)
PREAMBLE  = re.compile(r"^\s*\[Subagent hand-back\].*?The report follows:\s*", re.S)


def report_of(body):
    """에이전트 보고(<agent-message> 본문)에서 안내문을 떼고 들여쓰기를 푼 본문."""
    body = PREAMBLE.sub("", body)
    return "\n".join(l[2:] if l.startswith("  ") else l for l in body.strip().splitlines())
# 기계가 넣은 턴: 에이전트 보고, 완료 알림, 시스템 알림, 로컬 명령 출력. 사용자 요청이 아니다.
SYSTEM    = re.compile(r"\s*(<(agent-message|task-notification|system-reminder|local-command-caveat)\b"
                       r"|Another Claude session sent a message:|\[SYSTEM NOTIFICATION)")


def records(path, start, end=None):
    """transcript 의 [start, end) 구간에서 온전한 줄만 JSON 으로 읽는다. (레코드 목록, 다 읽은 위치)"""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read() if end is None else f.read(max(0, end - start))
    stop = data.rfind(b"\n") + 1                 # 쓰는 중인 마지막 줄은 다음에 읽는다
    out = []
    for line in data[:stop].splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("isSidechain"):
            out.append(r)
    return out, start + stop


def since_stop(path, st):
    """마지막 Stop 이후 transcript 를 읽어 (사건 목록, 마지막 응답, 끝난 도구 호출) 을 돌려준다.

    사건은 (종류, 정규화된 텍스트, 시각) 이다. user 는 직접 입력한 요청(type=user 의
    텍스트), 작업 중에 끼어든 대기열 요청(queued_command 첨부), 취소 표시 등이고,
    sys 는 기계가 넣은 턴(에이전트 보고·완료 알림 등), assistant 는 Claude 가 응답을
    시작했다는 표시다. 판단할 수 없으면 사건 목록은 None.
    끝난 도구 호출은 {tool_use_id: (상태, 에이전트의 보고)} 로, 메인 세션이 받은 결과(tool_result),
    백그라운드 에이전트의 보고(<agent-message>)와 완료 알림(<task-notification>)에서 모은다.
    보고는 agentId 로 오므로, 띄울 때의 tool_result 에서 agentId -> tool_use_id 를 배워 둔다.
    """
    try:
        recs, st["offset"] = records(path, st.get("offset", 0))
    except Exception:
        return None, None, {}
    seq, reply, finished = [], None, {}
    for r in recs:
        a = r.get("attachment") or {}
        if r.get("type") == "assistant":
            seq.append(("assistant", "", when(r)))
            reply = texts((r.get("message") or {}).get("content")).strip() or reply
            continue
        if r.get("type") == "user":
            content = (r.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out = json.dumps(b.get("content"), ensure_ascii=False)
                    m = LAUNCHED.search(out)
                    if m:                                    # 백그라운드 에이전트: 보고나 완료 알림으로 끝난다
                        st.setdefault("agent_ids", {})[m.group(1)] = b.get("tool_use_id")
                    else:                                    # 앞에서 실행된 에이전트: 도구 결과가 곧 보고다
                        finished[b.get("tool_use_id")] = ("failed" if b.get("is_error") else "completed",
                                                          texts(b.get("content")))
            c = texts(content)
        elif r.get("type") == "attachment" and a.get("type") == "queued_command":
            c = texts(a.get("prompt"))               # 이미지가 붙으면 블록 목록으로 온다
        else:
            continue
        if isinstance(c, str) and c.strip():
            for aid, body in HANDBACK.findall(c):    # 에이전트의 보고가 메인 세션에 전달됐다
                tid = (st.get("agent_ids") or {}).get(aid)
                if tid:
                    finished.setdefault(tid, ("completed", report_of(body)))
            if "<task-notification>" in c:           # 알림은 상태만 준다. 보고가 있으면 그대로 둔다
                for tid, status in TASK_DONE.findall(c):
                    finished[tid] = (status, (finished.get(tid) or (None, None))[1])
            seq.append(("sys" if SYSTEM.match(c) else "user", norm(c), when(r)))
    if not any(k in ("user", "sys") for k, _, _ in seq):  # 턴마다 요청이 최소 하나는 있다. 없으면 형식이 바뀐 것
        return None, None, finished
    return seq, reply, finished


INTERRUPT = "[Request interrupted by user"      # Esc 취소와 도구 거부("... for tool use") 둘 다


def classify(turns, seq, final=False):
    """열린 턴을 transcript 사건과 맞춰 (턴, 상태, 중단 시각) 목록을 돌려준다.

    상태: None(아직 전달 안 됨) / "interrupted"(취소 표시가 있음) /
    "cancelled"(응답이 시작되기 전에 다음 요청이 옴) / "ran"(처리됨).
    각 요청의 구간은 다음 요청 직전까지다. final 이면 마지막 요청도 구간이 끝난 것으로 본다.
    """
    pos, start = [], 0
    for t in turns:
        kind = "sys" if t.get("kind") == "handback" else "user"   # 보고에 답하는 턴은 기계 턴에서 찾는다
        i = next((i for i in range(start, len(seq))
                  if seq[i][0] == kind and t["key"] in seq[i][1]), None)
        pos.append(i)
        if i is not None:
            start = i                            # 대기열 요청 여럿이 한 메시지로 전달될 수 있다
    # 기계가 넣은 턴도 구간 경계다. 취소된 요청 뒤에 온 알림 턴의 응답이 그 요청의 것으로 잡히면 안 된다.
    marks = sorted({p for p in pos if p is not None} | {i for i, e in enumerate(seq) if e[0] == "sys"})
    out = []
    for t, p in zip(turns, pos):
        if p is None:
            out.append((t, None, None)); continue
        upto = next((m for m in marks if m > p), len(seq))
        seg = seq[p + 1:upto]
        stop = next((e for e in seg if e[0] == "user" and e[1].startswith(INTERRUPT)), None)
        if stop:
            out.append((t, "interrupted", stop[2] or time.time()))
        elif (upto < len(seq) or final) and not any(e[0] == "assistant" for e in seg):
            out.append((t, "cancelled", None))
        else:
            out.append((t, "ran", None))
    return out


LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def prose(para):
    """제목에 쓸 수 있는 문단이면 기호를 걷어낸 한 줄, 아니면(코드·표·목록 뒤 문단 등) None."""
    lines = para.strip().splitlines()
    if not lines or lines[0].lstrip().startswith(("```", "|")):
        return None
    if LIST_ITEM.match(lines[0]):
        para = lines[0]                          # 목록은 첫 항목만. 이어 붙이면 다음 항목 번호가 섞인다
    para = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", para)       # [x](url) -> x
    para = LIST_ITEM.sub("", para)
    para = re.sub(r"^\s*(?:#{1,6}|>)\s*", "", para, flags=re.M)  # 제목·인용 기호 (#1 같은 번호는 살린다)
    return norm(re.sub(r"[*`|]", "", para))


def headline(text):
    """응답의 첫 문장. 응답은 결론부터 쓰므로 그 턴의 작업과 결과를 요약한다.

    "고쳤습니다." 처럼 첫 문장이 짧아 내용이 없으면 다음 문장을 이어 붙인다.
    """
    paras = [p for p in text.strip().split("\n\n") if p.strip()]
    first = prose(paras[0]) if paras else None
    if not first:
        return ""
    heading = paras[0].lstrip().startswith("#")
    sents = re.split(r"(?<=[.!?])\s+", first)
    out = sents[0]
    if heading:                                  # 머리말은 그 자체로 제목이다
        return out
    rest = sents[1:] + [s for p in paras[1:2] if not p.lstrip().startswith("#")
                        for s in re.split(r"(?<=[.!?])\s+", prose(p) or "") if s]
    for s in rest:
        if len(out) >= 20:
            break
        out += " " + s
    return out

# ----------------------------------------------------------------- 훅


def h_start(ev, st, sid):
    root = conf("WORKFLOWY_ROOT_ID", "root_id")
    proj = pathlib.Path(ev.get("cwd") or ".").name
    why  = ev.get("session_start_reason") or ev.get("source") or "?"
    nid  = node(root, f"{proj} · {datetime.now():%Y-%m-%d %H:%M}",
                note=f"cwd: {ev.get('cwd')}\nsession: {sid}\nstart: {why}")
    tp = ev.get("transcript_path")                # resume 이면 이전 대화는 건너뛴다
    st.update(session_node=nid, started=time.time(),
              offset=os.path.getsize(tp) if tp and os.path.exists(tp) else 0)
    save(sid, st)
    print(f"[workflowy] 이 세션의 작업 로그: https://workflowy.com/#/{short(nid)}")
    # 플러그인은 사용자의 CLAUDE.md 를 건드릴 수 없다.
    # SessionStart 의 stdout 은 Claude 에게 전달되므로 여기서 상시 지시를 준다.
    print("사용자 요청은 턴 단위로 자동 기록된다.\n"
          "- 도구의 description 은 사용자와 대화하는 언어로, 지금 하는 일을 명사형으로 짧게 쓴다 "
          "(예: '설치본과 저장소 코드 비교'). 턴 아래에 진행 단계로 그대로 기록된다.\n"
          "- 다음 시점에는 /workflowy:session-log 스킬에 한 문장을 인자로 넘겨 메모를 남긴다: "
          "여러 단계 작업 착수 직전(계획), 접근 방식을 바꿨을 때(이유), 막혔을 때(무엇에 왜), "
          "사용자가 방향을 정했을 때(결정). 스킬을 부르는 것만으로 기록되니 명령을 따로 실행하지 않는다.\n"
          "- 기록 사실 자체는 사용자에게 보고하지 않는다.")
    new = new_errors()
    if new:
        print(f"[workflowy] 지난 확인 이후 기록 오류 {len(new)}건. 마지막: {new[-1]}\n"
              "사용자에게 알리고, 자세한 점검은 /workflowy:session-log doctor 로 한다.")


def new_errors():
    """지난 세션 시작 이후 쌓인 오류 줄. 기록 실패가 조용히 묻히지 않게 한 번씩 알린다."""
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


def close(t, text, st, until=None, note=None):
    """턴의 제목을 확정한다. text 가 없으면 요청 앞부분을, until 이 있으면 소요 시간을 붙인다."""
    name = f"{t['hm']} {text}" if text and t.get("hm") else t["label"]
    if until:
        name += duration(until - max(t["ts"], st.get("last_stop", 0)))
    edit(t["id"], name=name, **({"note": note} if note else {}))
    t["closed"] = True                           # 다음 Stop 까지 구간 경계로 남겨 둔다


def duration(secs):
    """소요 시간 표시. 1분 미만은 정보가 없어 붙이지 않는다."""
    return f" <i>· {secs / 60:.0f}분</i>" if secs >= 60 else ""


def h_prompt(ev, st, sid):
    if not st.get("session_node"):
        return None
    turns = st.setdefault("turns", [])
    p = ev.get("prompt") or ""
    m = SKILL.match(p.strip())
    if m:                                        # 사용자가 직접 부른 session-log 는 요청이 아니라 기록 명령이다
        return session_cmd(m.group(1), st, sid)
    # 기계가 넣은 턴은 요청이 아니므로 기록하지 않는다 (source 필드가 없는 버전은 내용으로 판단).
    # 다만 에이전트의 보고에 메인 세션이 답하는 턴은 메인 세션의 일이므로 턴으로 남긴다.
    # 작업 중에 끼어든 보고는 진행 중인 턴 안에서 처리되므로 따로 만들지 않는다.
    if ev.get("source", "user") != "user" or SYSTEM.match(p):
        m = HANDBACK.search(p)
        if not m or any(not t.get("closed") for t in turns):
            return None
        a = (st.get("agents") or {}).get((st.get("agent_ids") or {}).get(m.group(1)), {})
        desc = a.get("title", "에이전트").split(": ", 1)[-1]
        hm = f"{datetime.now():%H:%M}"
        # 계기(어느 보고를 받았는지)는 진행 중일 때만 보인다. 답변이 그 보고에 관한 것이라는 뜻은 아니므로
        # 끝나면 노트에는 답변 전문만 남긴다 (여러 보고를 취합한 답변일 수 있다).
        nid = node(st["session_node"], hm, note=f"\U0001f916 {desc} 의 보고를 받아 작업 중")
        turns.append({"id": nid, "hm": hm, "label": f"{hm} \U0001f916 {html_label(desc, 100)} 의 보고에 답함",
                      "kind": "handback", "agent": desc, "key": f'<agent-message from="{m.group(1)}"',
                      "ts": time.time()})
        save(sid, st)
        return None
    busy = False
    if any(not t.get("closed") for t in turns):
        # 취소(Esc)나 도구 거부로 끝난 턴에는 Stop 이 오지 않으므로 다음 요청이 올 때 닫는다.
        # offset 은 Stop 만 옮긴다. 여기서는 복사본으로 읽기만 한다.
        seq, _, _ = since_stop(ev.get("transcript_path"), dict(st))
        for t, state, cut in classify(turns, seq or []):
            if state == "interrupted" and not t.get("closed"):
                close(t, "중단됨", st, cut)
        busy = any(not t.get("closed") for t in turns)
    # 작업 중에 대기열에 넣은 요청도 이 훅은 넣는 순간 한 번만 불린다(꺼낼 때는 안 불린다).
    # 그래서 열린 턴을 덮어쓰지 않고 목록에 쌓아 두고, 제목은 Stop 에서 전달 여부로 판단해 확정한다.
    # 요청 전문은 노트에 있으므로 제목은 시각만 두고, 턴이 끝나면 응답의 첫 문장으로 채운다.
    p = norm(ev.get("prompt"))
    hm = f"{datetime.now():%H:%M}"
    nid = node(st["session_node"], hm, note=scrub(ev.get("prompt"), 2000, lines=True))
    turns.append({
        "id": nid, "hm": hm, "label": f"{hm} " + html_label(ev.get("prompt"), 110),
        # 슬래시 명령은 transcript 에 이름과 인자가 따로 남으므로 이름만 맞춘다
        "key": p.split(" ")[0] if p.startswith("/") else p[:40], "ts": time.time(),
        # 작업 중에 넣은 요청: 진행 단계는 아직 하던 턴에 붙인다
        **({"queued": True} if busy else {})})
    save(sid, st)
    return None


def h_stop(ev, st, sid):
    tp, frm = ev.get("transcript_path"), st.get("offset", 0)
    seq, reply, finished = since_stop(tp, st)
    reply = ev.get("last_assistant_message") or reply
    now = time.time()
    # 에이전트의 결과를 받아 응답한 메인 세션이 그 응답으로 보고한다.
    # 보고·완료 알림으로 시작된 턴은 턴 노드가 없지만(h_prompt 가 걸러냄) 여기서 처리된다.
    agents = st.get("agents") or {}
    for tid, (status, report) in finished.items():
        if tid in agents:
            finish_agent(agents.pop(tid), now, status, report)
    turns = st.get("turns") or []
    if not turns:
        st["last_stop"] = now
        save(sid, st)
        return
    # transcript 를 읽을 수 없으면 전부 처리된 것으로 본다.
    rows = classify(turns, seq) if seq else [(t, "ran", None) for t in turns]
    titled, left, steps = None, [], []
    for t, state, cut in rows:
        if t.get("closed"):
            continue
        if state is None:
            # 아직 전달 안 된 대기열 요청은 다음 턴이 된다. 그 아래 붙은 단계는 이번 턴의 것이다.
            # 두 번째 Stop 까지 못 맞추면 요청 앞부분으로 확정한다.
            steps += t.pop("steps", []); t.pop("queued", None)
            t["waits"] = t.get("waits", 0) + 1
            if t["waits"] < 2:
                left.append(t); continue
            close(t, None, st)
        elif state == "interrupted": close(t, "중단됨", st, cut)
        elif state == "cancelled":   close(t, "취소됨", st)
        elif titled:
            close(t, "↳ 앞 요청과 함께 처리", st, now); steps += t.get("steps", [])
        elif reply:
            # 보고에 답한 턴은 요청이 없으므로 노트에 답변 전문을 남긴다 (요청 턴의 노트는 요청 전문)
            note = plain(scrub(reply, 6000, lines=True)) if t.get("kind") == "handback" else None
            close(t, html_label(headline(reply), 100), st, now, note)
            titled = t; steps += t.get("steps", [])
        else:
            close(t, None, st, now)              # 응답을 못 찾으면 요청 앞부분을 쓴다
    if titled and tp:
        # 턴이 끝난 뒤 결과까지 담은 목록을 쓰고 진행 중에 남긴 단계는 한데 모은다 (refine 훅이 비동기로 처리)
        st.setdefault("refine", []).append({
            "turn": titled["id"], "key": titled["key"], "handback": titled.get("kind") == "handback",
            "steps": steps, "path": tp, "from": frm, "to": st["offset"]})
    st.update(turns=left, last_stop=now)
    save(sid, st)


def h_idle(ev, st, sid):
    """입력 대기 알림(idle_prompt): Claude 가 쉬고 있으니 Stop 없이 끝난 턴은 취소·중단된 것이다.

    취소(Esc)에는 훅이 없어서, 사용자가 다음 요청을 보내기 전에는 이 알림
    (입력 없이 약 60초)이 그렇게 표시할 수 있는 가장 이른 신호다.
    """
    turns = st.get("turns") or []
    if not any(not t.get("closed") for t in turns):
        return
    seq, _, _ = since_stop(ev.get("transcript_path"), dict(st))   # offset 은 Stop 만 옮긴다
    for t, state, cut in classify(turns, seq or [], final=True):
        if t.get("closed") or state is None:
            continue
        close(t, "취소됨" if state == "cancelled" else "중단됨", st, cut)
    save(sid, st)


def h_end(ev, st, sid):
    nid = st.get("session_node")
    if not nid:
        return
    now = time.time()
    for a in (st.get("agents") or {}).values():   # 아직 일하던 에이전트는 세션과 함께 끝난다
        try: finish_agent(a, now, "stopped")
        except Exception: pass
    turns = st.get("turns") or []
    if any(not t.get("closed") for t in turns):
        seq, _, _ = since_stop(ev.get("transcript_path"), st)
        rows = classify(turns, seq, final=True) if seq else [(t, "ran", None) for t in turns]
        # 처리 중이던 턴(Stop 없이 세션이 끝남)도 중단된 것으로 본다
        text = {None: "처리되지 않음", "interrupted": "중단됨", "cancelled": "취소됨", "ran": "중단됨"}
        for t, state, cut in rows:
            if t.get("closed"):
                continue
            try: close(t, text[state], st, cut or (now if state == "ran" else None))
            except Exception: pass
    mins = (now - st.get("started", now)) / 60
    node(nid, f"⏹ 종료 · {ev.get('reason','?')} · {mins:.0f}분")
    spath(sid).unlink(missing_ok=True)


def current(st):
    """메모를 붙일 곳: 열린 턴 중 마지막, 없으면 세션."""
    return next((t["id"] for t in reversed(st.get("turns") or []) if not t.get("closed")),
                st.get("session_node"))


def working(st):
    """진행 단계를 붙일 턴: 작업 중에 대기열에 넣은 요청이 아니라 지금 처리 중인 턴."""
    open_ = [t for t in st.get("turns") or [] if not t.get("closed")]
    return next((t for t in reversed(open_) if not t.get("queued")), open_[-1] if open_ else None)


def h_note(text, st, sid):
    parent = current(st)
    if not parent:
        raise RuntimeError("기록 중인 세션이 없음 (세션 시작 훅이 실패했을 수 있음)")
    node(parent, "▸ " + label(text, 300),
         note=scrub(text, 2000, lines=True) if len(text) > 300 else None)


# ----------------------------------------------------------------- session-log 스킬
# Claude 가 Skill 도구로 부르면 PostToolUse, 사용자가 /workflowy:session-log 로 입력하면
# UserPromptSubmit 이 인자를 받는다. 둘 다 훅이라 API key 와 상태에 접근할 수 있다.


def session_cmd(args, st, sid):
    """스킬 인자를 처리하고 Claude 에게 돌려줄 말을 만든다. 메모가 성공하면 아무 말도 하지 않는다."""
    a = (args or "").strip()
    cmd, _, rest = a.partition(" ")
    if cmd in ("", "link"):
        nid = st.get("session_node")
        return f"[workflowy] 이 세션의 작업 로그: https://workflowy.com/#/{short(nid)}" if nid \
            else "[workflowy] 기록 중인 세션이 없습니다."
    if cmd == "close":
        h_end({"reason": "manual"}, st, sid)
        return "[workflowy] 세션 기록을 마감했습니다."
    if cmd == "doctor":
        buf = io.StringIO()
        with redirect_stdout(buf):
            do_doctor()
        return "[workflowy] 점검 결과\n" + buf.getvalue()
    text = rest.strip() if cmd == "note" else a
    if not text:
        return "[workflowy] 메모 내용이 비어 있어 기록하지 않았습니다."
    h_note(text, st, sid)
    return None


def h_skill(ev, st, sid):
    i = ev.get("tool_input") or {}
    if ev.get("agent_id") or not str(i.get("skill", "")).endswith("session-log"):
        return None
    return session_cmd(i.get("args"), st, sid)


# ----------------------------------------------------------------- 진행 단계
# 도구를 부를 때마다 Claude 가 붙이는 description 이 곧 "지금 하는 일" 이다.
# 도구가 시작될 때(PreToolUse) 그 문구를 진행 중인 턴 아래에 단계로 붙이고,
# 턴이 끝나면 refine 훅이 결과까지 담은 명사형 목록을 턴에 붙이고, 원본 단계는 "진행 단계" 아래로 옮긴다.


def step_of(ev):
    """단계로 남길 문구. 없으면 None. 잠금·상태 없이 판단할 수 있어야 한다(도구마다 불린다)."""
    if ev.get("agent_id"):
        return None                              # 서브에이전트가 부른 도구는 메인 세션의 단계가 아니다
    i = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    d = norm(i.get("description"))
    if not d or ev.get("tool_name") == "Skill":
        return None
    return ("\U0001f916 " if ev.get("tool_name") in ("Agent", "Task") else "") + d


def h_pre_tool(ev, st, sid):
    if ev.get("agent_id") or not st.get("session_node"):
        return                                   # 서브에이전트가 띄운 것은 메인 세션의 일이 아니다
    if ev.get("tool_name") in ("Agent", "Task"):
        agent_launch(ev, st)
    d, t = step_of(ev), working(st)
    if d and t and d != t.get("last_step"):
        t.setdefault("steps", []).append(node(t["id"], label(d, 200)))
        t["last_step"] = d
    save(sid, st)


SUMMARY = """너는 작업 기록을 정리한다. 입력은 AI 코딩 에이전트가 사용자 요청 하나를 처리한 과정이다
(요청, 에이전트의 중간 설명, 도구 호출과 결과, 최종 응답).
에이전트가 한 일을 순서대로 한 줄에 하나씩 나열하라.

- 한 줄은 60자 안팎, 명사형 어미로 간결하게 끝낸다. 예: "설치된 1.9.1과 저장소 코드 비교, 동일함 확인",
  "서브에이전트에게 공식 문서 확인 위임", "문제점 6건과 개선안 정리, 결정 사항 질문"
- 의미 있는 결과나 발견이 있으면 쉼표 뒤에 덧붙인다. 도구 결과·중간 설명·최종 응답에 드러난 것만 쓰고,
  드러나지 않은 결론은 짐작해 쓰지 않는다. 결과가 분명하지 않으면 한 일만 쓴다.
- 같은 목적의 연이은 도구 호출은 한 줄로 합친다. 명령어나 경로를 옮기지 말고 무엇을 했는지 쓴다.
- 마지막 줄은 최종 응답에서 사용자에게 한 일(보고·제안·질문)이다.
- 사용자 요청과 같은 언어로 쓴다.
- 목록만 출력한다. 번호·기호·머리말·맺음말을 붙이지 않는다."""


def brief(i):
    """description 이 없는 도구 호출(Read·Edit 등)의 대상."""
    for k in ("file_path", "notebook_path", "pattern", "url", "query", "skill", "command", "prompt"):
        if isinstance(i, dict) and i.get(k):
            v = str(i[k])
            return pathlib.PureWindowsPath(v).name if k.endswith("path") else norm(v)[:100]
    return ""


def digest(job):
    """refine 입력: 턴 구간의 transcript 를 요청·설명·도구·결과·응답으로 줄인 글. 도구 호출 수도 센다."""
    recs, _ = records(job["path"], job["from"], job["to"])
    start = 0
    for n, r in enumerate(recs):                 # 이 턴의 요청부터 (앞의 중단된 턴 작업은 뺀다)
        c = texts((r.get("message") or {}).get("content")) if r.get("type") == "user" else ""
        if c and job["key"] in norm(c):
            start = n; break
    out, tools, said = [], 0, None
    for r in recs[start:]:
        content = (r.get("message") or {}).get("content")
        if r.get("type") == "assistant":
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    said, final = len(out), b["text"]
                    out.append("(설명) " + scrub(b["text"], 400))
                elif b.get("type") == "tool_use":
                    tools += 1
                    i = b.get("input") or {}
                    out.append(f"(도구) {b.get('name')}: " + (norm(i.get("description")) or brief(i)))
        elif r.get("type") == "user" and not r.get("isMeta"):   # isMeta: 불러온 스킬 본문 등
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    res = texts(b.get("content")) or str(b.get("content") or "")
                    out.append("  -> " + scrub(norm(res), 300))
            c = texts(content)
            if c.strip():
                out.append(("[요청]\n" + scrub(c, 1500, lines=True)) if not out else
                           ("(보고 도착) " if SYSTEM.match(c) else "(사용자) ") + scrub(norm(c), 300))
    if said is not None:                         # 마지막 설명이 최종 응답이다. 결론이 여기 있으므로 길게 둔다
        out[said] = "[최종 응답]\n" + scrub(final, 2500, lines=True)
    text = "\n".join(out)
    return (text[:30000] + "\n…(생략)") if len(text) > 30000 else text, tools


def summarize(text):
    """Haiku 로 명사형 단계 목록을 만든다. 이 호출은 사용자의 Claude 계정으로 청구된다."""
    exe = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
    if not exe:
        raise RuntimeError("claude 실행 파일을 찾지 못함")
    # 부모 세션의 흔적을 지워 독립된 세션으로 띄운다. 사용자 설정을 읽지 않으므로 플러그인·훅도 뜨지 않고,
    # WF_DISABLE 은 그래도 이 플러그인이 뜰 경우 기록하지 않게 하는 안전장치다.
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDECODE" and not k.startswith(("CLAUDE_CODE_", "CLAUDE_PLUGIN_"))}
    # 확장 사고를 끈다. 켜 두면 목록 몇 줄에 수천 토큰을 생각하느라 40초 이상 걸린다 (끄면 10초 안팎).
    env.update(WF_DISABLE="1", MAX_THINKING_TOKENS="0")
    r = subprocess.run(
        [exe, "-p", "--model", "haiku", "--no-session-persistence", "--setting-sources", "project",
         "--settings", '{"alwaysThinkingEnabled": false}',
         "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--system-prompt", SUMMARY],
        input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=100, cwd=str(STATE), env=env)
    if r.returncode:
        raise RuntimeError(f"claude -p 실패 ({r.returncode}): {norm(r.stderr or r.stdout)[:200]}")
    lines = [LIST_ITEM.sub("", l).strip(" •·") for l in r.stdout.splitlines()]
    # 프롬프트로 막아도 "… 작업 기록:" 같은 머리말이나 "## 제목" 이 붙어 나올 때가 있다
    return [l for l in lines if l and not l.endswith((":", "：")) and not l.startswith("#")][:40]


def refine_one(job):
    text, tools = digest(job)
    if not tools:
        return                                   # 도구를 쓰지 않은 턴은 응답이 곧 전부다
    lines = summarize(text)
    if not lines:
        return
    # 턴은 위에서 아래로 시간순으로 읽혀야 한다. 목록은 턴 중에 남긴 ▸ 메모 뒤에 차례로 붙이고,
    # 원본 단계는 그 뒤의 "진행 단계" 로 옮긴다. 목록 안에서 메모의 자리는 알 수 없어 메모가 목록보다 먼저 온다.
    for s in lines:
        node(job["turn"], label(s, 200))
    keep_steps(job["turn"], job["steps"])


def keep_steps(turn, steps):
    """원본 단계는 지우지 않고 턴 맨 아래의 "진행 단계" 노드 하나로 옮긴다. 정리된 목록과 섞이지 않고,
    대기열 요청이나 합쳐진 턴 아래 붙었던 단계도 이 턴으로 돌아온다. 접힘 상태는 API 로 정할 수 없다."""
    if not steps:
        return
    box, moved = node(turn, f"진행 단계 · {len(steps)}"), 0
    for nid in steps:
        try:
            call("POST", f"/nodes/{nid}/move", {"parent_id": box, "position": "bottom"})
            moved += 1
        except urllib.error.HTTPError as e:
            if e.code != 404: raise              # 사용자가 이미 지운 단계
    if not moved:
        call("DELETE", f"/nodes/{box}")
    elif moved != len(steps):
        edit(box, name=f"진행 단계 · {moved}")


def do_refine(ev, sid):
    """Stop 과 함께 뜨는 비동기 훅. stop 훅이 넘겨준 정리 작업을 가져와 처리한다.

    두 훅은 동시에 시작되므로, stop 훅이 이번 Stop 을 처리할 때까지 잠시 기다린다.
    stop 훅은 transcript 를 끝까지 읽고 그 위치를 남기므로, 그 위치가 지금 크기에 닿았으면 처리가 끝난 것이다.
    """
    began, jobs = time.time(), []
    try:    size = os.path.getsize(ev.get("transcript_path") or "")
    except OSError: size = 0
    while True:
        lk = lock(sid)
        try:
            st = load(sid)
            jobs = st.pop("refine", [])
            if jobs:
                save(sid, st)
            done = not st or st.get("offset", 0) >= size
        finally:
            if lk: lk.unlink(missing_ok=True)
        if jobs or done or time.time() - began > 15:
            break
        time.sleep(0.3)
    for job in jobs:
        try:
            refine_one(job)
        except Exception as e:
            log_error("refine", e)


# 서브에이전트: 일은 서브에이전트가 하고 기록은 메인 세션이 한다.
# 메인 세션이 에이전트에게 맡기는 순간(PreToolUse) "진행 중" 항목을 만들고, 에이전트의 보고가
# 도착한 뒤의 Stop 에서 그 보고를 노트에 남기고 제목을 확정한다. 둘은 tool_use_id 로 잇는다.
# 보고를 받아 메인 세션이 한 일(번역·정리 등)은 에이전트 항목이 아니라 메인 세션의 턴으로 남긴다.
# 에이전트는 턴이 끝난 뒤에도 일할 수 있고 턴 노드는 접혀 보이므로, 세션 바로 아래(턴과 같은 단계)에 둔다.


def agent_launch(ev, st):
    i = ev.get("tool_input") or {}
    head = f"{datetime.now():%H:%M} \U0001f916 {i.get('subagent_type') or 'general-purpose'}: "
    ask = scrub(i.get("prompt"), 1500, lines=True)
    nid = node(st["session_node"], head + label(i.get("description"), 100) + " · 진행 중",
               note="지시: " + ask)
    st.setdefault("agents", {})[ev.get("tool_use_id")] = {
        "id": nid, "title": head + html_label(i.get("description"), 100), "ts": time.time(), "ask": ask}


def finish_agent(a, until, status="completed", report=None):
    """'진행 중' 을 결과로 바꾸고, 에이전트의 보고를 노트 첫 줄에 남겨 접혀 있어도 보이게 한다."""
    if status == "completed":
        tail = duration(until - a["ts"])
    else:
        tail = f" <i>· {({'failed': '실패', 'killed': '중단됨', 'stopped': '중단됨'}).get(status, status)}</i>"
    kw = {"name": a["title"] + tail}
    if report:
        kw["note"] = f"결과: {plain(scrub(report, 6000, lines=True))}\n\n지시: {a.get('ask', '')}"
    edit(a["id"], **kw)

# ----------------------------------------------------------------- 점검


def do_doctor():
    ok = True
    key, root = conf("WORKFLOWY_API_KEY", "api_key"), conf("WORKFLOWY_ROOT_ID", "root_id")
    print(f"  {'ok ' if key else 'FAIL'} API key      {'설정됨' if key else '없음'}")
    print(f"  {'ok ' if root else 'FAIL'} root id      {root or '없음'}")
    print(f"  ok  상태 저장    {STATE}")
    exe = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
    print(f"  {'ok ' if exe else '주의'} 단계 정리    {exe or 'claude 실행 파일 없음 (진행 단계를 정리하지 못함)'}")
    ok &= bool(key and root)
    if key and root:
        try:
            t0 = time.time()
            nm = call("GET", f"/nodes/{root}")["node"]["name"]
            print(f"  ok  API 연결     '{nm}' ({time.time()-t0:.2f}초)")
        except urllib.error.HTTPError as e:
            code = {401: "API key가 잘못됨", 403: "권한 없음",
                    404: "root id를 찾을 수 없음"}.get(e.code, f"HTTP {e.code}")
            print(f"  FAIL API 연결     {code}"); ok = False
        except Exception as e:
            print(f"  FAIL API 연결     {type(e).__name__}: {e}"); ok = False

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
    # 입력의 깨진 바이트는 서로게이트가 되어 API 가 500 을 내므로 치환한다.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if os.environ.get("WF_DISABLE"):
        sys.exit(0)                              # 단계 정리용 claude -p 세션은 기록하지 않는다

    mode = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    if not STATE:
        print("[workflowy] 플러그인 훅 밖에서 실행되어 API key 와 기록 상태를 쓸 수 없습니다.\n"
              "메모·링크·마감·점검은 /workflowy:session-log 스킬로 하세요 (예: /workflowy:session-log doctor).",
              file=sys.stderr)
        sys.exit(1)
    if mode == "doctor":
        sys.exit(do_doctor())

    try: ev = json.loads(sys.stdin.read() or "{}")
    except Exception: ev = {}
    sid = ev.get("session_id", "")
    if mode == "pre-tool" and not step_of(ev) and ev.get("tool_name") not in ("Agent", "Task"):
        sys.exit(0)                              # 모든 도구마다 불리므로 할 일이 없으면 잠금도 잡지 않는다
    if mode == "refine":
        do_refine(ev, sid)
        sys.exit(0)

    say = None
    lk = lock(sid)
    try:
        st = load(sid)
        if   mode == "session-start": h_start(ev, st, sid)
        elif mode == "prompt":        say = h_prompt(ev, st, sid)
        elif mode == "stop":          h_stop(ev, st, sid)
        elif mode == "idle":          h_idle(ev, st, sid)
        elif mode == "pre-tool":      h_pre_tool(ev, st, sid)
        elif mode == "skill":         say = h_skill(ev, st, sid)
        elif mode == "session-end":   h_end(ev, st, sid)
    except Exception as e:
        log_error(mode, e)
        if mode == "skill" or (mode == "prompt" and SKILL.match((ev.get("prompt") or "").strip())):
            say = f"[workflowy] 기록 실패: {type(e).__name__}: {e}"
    finally:
        if lk:
            lk.unlink(missing_ok=True)
    if say and mode == "skill":                  # Claude 에게 결과를 돌려준다 (스킬 본문 뒤에 붙는다)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": say}}, ensure_ascii=False))
    elif say:                                    # UserPromptSubmit 의 stdout 은 Claude 의 컨텍스트가 된다
        print(say)
    sys.exit(0)   # 훅은 무슨 일이 있어도 0. exit 2는 세션을 차단한다.


if __name__ == "__main__":
    main()
