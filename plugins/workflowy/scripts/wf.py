#!/usr/bin/env python3
"""
wf.py - Claude Code 세션을 Workflowy에 실시간 기록한다.

설치 (머신당 1회):
    mkdir -p ~/.claude/hooks && cp wf.py ~/.claude/hooks/wf.py
    python3 ~/.claude/hooks/wf.py install --key <API_KEY> --root <SHORT_ID>

점검:   python3 ~/.claude/hooks/wf.py doctor
제거:   python3 ~/.claude/hooks/wf.py uninstall
"""
import html, json, os, re, shutil, sys, time, pathlib, urllib.request, urllib.error
from datetime import datetime

API    = "https://workflowy.com/api/v1"
HOME   = pathlib.Path.home()
CFG    = HOME / ".config" / "workflowy"
CLAUDE = HOME / ".claude"
DEST   = CLAUDE / "hooks" / "wf.py"
# 플러그인으로 설치되면 CLAUDE_PLUGIN_DATA(업데이트에도 보존되는 영역)를 쓴다.
PLUGIN = bool(os.environ.get("CLAUDE_PLUGIN_ROOT"))
STATE  = pathlib.Path(os.environ.get("CLAUDE_PLUGIN_DATA") or (CLAUDE / "workflowy"))
ERRLOG = STATE / "error.log"
MARK   = "<!-- wf-workflowy-logger -->"

SECRET = re.compile(
    r"sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}|(?i:bearer)\s+[A-Za-z0-9._\-]{20,}"
    r"|(?i:api[_\-]?key|token|password|secret)\s*[=:]\s*\S{8,}")

# name 필드는 마크다운을 파싱한다. 백슬래시 이스케이프가 통하는 문자들.
MD = re.compile(r"([*`\[\]])")

CMD = 'python3 "$HOME/.claude/hooks/wf.py"'

HOOKS = {
    "SessionStart": [{"matcher": "startup|resume|clear", "hooks": [
        {"type": "command", "timeout": 15, "command": f"{CMD} session-start"}]}],
    "UserPromptSubmit": [{"hooks": [
        {"type": "command", "timeout": 10, "command": f"{CMD} prompt"}]}],
    "Stop": [{"hooks": [
        {"type": "command", "timeout": 10, "command": f"{CMD} stop"}]}],
    "SessionEnd": [{"hooks": [
        {"type": "command", "timeout": 10, "command": f"{CMD} session-end"}]}],
}

SKILL = """---
name: session-log
description: 현재 Claude Code 세션의 Workflowy 작업 로그에 의미 있는 메모를 남기거나, 세션 노드 링크를 확인하거나, 세션을 수동으로 마감한다. 계획을 세웠을 때 / 중요한 결정이나 발견이 있을 때 / 막혔을 때 / 사용자가 "workflowy에 기록해"라고 할 때 사용한다.
argument-hint: [note <텍스트> | link | close]
---

# Workflowy 작업 로그

사용자 요청은 훅이 턴 단위로 자동 기록한다.
이 스킬은 훅이 알 수 없는 **의미 단위 정보**를 기록한다.

현재 세션 ID는 `${CLAUDE_SESSION_ID}` 이다.

## 명령

메모 추가 - 진행 중인 턴 아래에 불릿으로 붙는다:

```bash
python3 ~/.claude/hooks/wf.py note "${CLAUDE_SESSION_ID}" "계획: 인증을 3단계로 분리"
```

세션 노드 링크 확인:

```bash
python3 ~/.claude/hooks/wf.py link "${CLAUDE_SESSION_ID}"
```

세션 수동 마감 - SessionEnd 훅이 뜨지 않았을 때(강제 종료 등):

```bash
python3 ~/.claude/hooks/wf.py close "${CLAUDE_SESSION_ID}"
```

## 무엇을 기록할 가치가 있는가

**기록한다**: 착수 전 계획 / 방향을 바꾼 이유 / 예상 밖의 발견 /
막힌 지점과 그 원인 / 사용자가 내린 결정.

**기록하지 않는다**: 파일을 읽었다·명령을 실행했다 같은 단순 사실 /
한 줄짜리 진행 중계 / 최종 요약(대화에 이미 있다).

메모 하나는 한 문장. 길어지면 여러 개로 나눈다.
기록 사실 자체를 사용자에게 보고하지 말고, 조용히 남기고 하던 일을 계속한다.
"""

MEMO = f"""
{MARK}
## Workflowy 작업 로그

이 세션의 사용자 요청은 Workflowy에 턴 단위로 자동 기록된다. 다음 시점에 `session-log` 스킬로 메모를 남겨라:

- 여러 단계 작업을 시작하기 직전 - 계획 한 줄
- 접근 방식을 바꿨을 때 - 바꾼 이유
- 막혔을 때 - 무엇에 왜 막혔는지
- 사용자가 방향을 결정했을 때 - 결정 내용

기록 자체를 사용자에게 보고하지 마라. 조용히 남기고 하던 일을 계속하라.
{MARK}
"""

# ----------------------------------------------------------------- 공통


def conf(env, fname):
    """우선순위: 환경변수 -> 플러그인 userConfig -> ~/.config/workflowy 파일."""
    v = os.environ.get(env)
    if v:
        return v.strip()
    # 플러그인 userConfig. 문서상 대소문자 표기가 엇갈려 둘 다 확인한다.
    want = "claude_plugin_option_" + fname
    for k, val in os.environ.items():
        if k.lower() == want and val.strip():
            return val.strip()
    p = CFG / fname
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def scrub(s, n=400):
    s = SECRET.sub("<<redacted>>", str(s or "")).replace("\n", " / ")
    return (s[:n] + "…") if len(s) > n else s


def label(s, n=400):
    """create용: name 필드의 마크다운 파싱을 중화한다."""
    return MD.sub(r"\\\1", scrub(s, n)).replace("~~", "∼∼")


def html_label(s, n=400):
    """update용: POST /nodes/:id 는 마크다운이 아니라 HTML만 해석한다."""
    return html.escape(scrub(s, n), quote=False)


def call(method, path, body=None, tries=3):
    key = conf("WORKFLOWY_API_KEY", "api_key")
    if not key:
        raise RuntimeError("Workflowy API key 없음 (install 을 먼저 실행하세요)")
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


def node(parent, name, note=None, layout=None, pos="bottom"):
    b = {"parent_id": parent, "name": name, "position": pos}
    if note:   b["note"] = note[:8000]
    if layout: b["layoutMode"] = layout
    return call("POST", "/nodes", b)["item_id"]


def edit(nid, **kw): call("POST", f"/nodes/{nid}", kw)
def done(nid):       call("POST", f"/nodes/{nid}/complete")
def short(nid):      return nid.split("-")[-1]

def spath(sid): return STATE / "state" / f"{sid}.json"

def load(sid):
    try:    return json.loads(spath(sid).read_text(encoding="utf-8"))
    except Exception: return {}

def save(sid, st):
    p = spath(sid); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st), encoding="utf-8")


def norm(s): return " ".join(str(s or "").split())


def texts(content):
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else ""


def since_stop(path, st):
    """마지막 Stop 이후 transcript 를 읽어 (사용자 요청들, 마지막 응답) 을 돌려준다.

    요청은 정규화된 텍스트 목록이다. 직접 입력한 요청은 type=user 의 텍스트로,
    작업 중에 끼어든 대기열 요청은 queued_command 첨부로 남는다.
    판단할 수 없으면 (None, None).
    """
    try:
        with open(path, "rb") as f:
            f.seek(st.get("offset", 0))
            data = f.read()
    except Exception:
        return None, None
    end = data.rfind(b"\n") + 1                  # 쓰는 중인 마지막 줄은 다음 Stop 에서 읽는다
    st["offset"] = st.get("offset", 0) + end
    got, reply = [], None
    for line in data[:end].splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("isSidechain"):
            continue
        a = r.get("attachment") or {}
        if r.get("type") == "assistant":
            reply = texts((r.get("message") or {}).get("content")).strip() or reply
            continue
        if r.get("type") == "user":
            c = texts((r.get("message") or {}).get("content"))
        elif r.get("type") == "attachment" and a.get("type") == "queued_command":
            c = a.get("prompt")
        else:
            continue
        if isinstance(c, str) and c.strip():
            got.append(norm(c))
    if not got:                                  # 턴마다 요청이 최소 하나는 있다. 없으면 형식이 바뀐 것
        return None, None
    return got, reply


def headline(text):
    """응답의 첫 문장. 응답은 결론부터 쓰므로 그 턴의 작업과 결과를 요약한다."""
    para = text.strip().split("\n\n")[0]
    para = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", para)       # [x](url) -> x
    para = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", para)          # 목록 기호
    para = norm(re.sub(r"[*`#>|]", "", para))                    # 마크다운 기호
    return re.split(r"(?<=[.!?])\s", para, maxsplit=1)[0]

# ----------------------------------------------------------------- 훅


def h_start(ev, st, sid):
    root = conf("WORKFLOWY_ROOT_ID", "root_id")
    proj = pathlib.Path(ev.get("cwd") or ".").name
    why  = ev.get("session_start_reason") or ev.get("source") or "?"
    nid  = node(root, f"## {proj} · {datetime.now():%Y-%m-%d %H:%M}",
                note=f"cwd: {ev.get('cwd')}\nsession: {sid}\nstart: {why}")
    tp = ev.get("transcript_path")                # resume 이면 이전 대화는 건너뛴다
    st.update(session_node=nid, started=time.time(),
              offset=os.path.getsize(tp) if tp and os.path.exists(tp) else 0)
    save(sid, st)
    print(f"[workflowy] 이 세션의 작업 로그: https://workflowy.com/#/{short(nid)}")
    if PLUGIN:
        # 플러그인은 사용자의 CLAUDE.md 를 건드릴 수 없다.
        # SessionStart 의 stdout 은 Claude 에게 전달되므로 여기서 상시 지시를 준다.
        print("사용자 요청은 턴 단위로 자동 기록된다. 다음 시점에는 /workflowy:session-log 스킬로 "
              "한 문장 메모를 남겨라: 여러 단계 작업 착수 직전(계획), 접근 방식을 "
              "바꿨을 때(이유), 막혔을 때(무엇에 왜), 사용자가 방향을 정했을 때(결정). "
              "기록 사실 자체는 사용자에게 보고하지 말 것.")


def h_prompt(ev, st, sid):
    if not st.get("session_node"):
        return
    # 작업 중에 대기열에 넣은 요청도 이 훅은 넣는 순간 한 번만 불린다(꺼낼 때는 안 불린다).
    # 그래서 열린 턴을 덮어쓰지 않고 목록에 쌓아 두고, 완료는 Stop 에서 전달 여부로 판단한다.
    # 요청 전문은 노트에 있으므로 제목은 시각만 두고, 턴이 끝나면 응답의 첫 문장으로 채운다.
    hm = f"{datetime.now():%H:%M}"
    nid = node(st["session_node"], hm, note=scrub(ev.get("prompt"), 2000), layout="todo")
    st.setdefault("turns", []).append({
        "id": nid, "hm": hm, "label": f"{hm} " + html_label(ev.get("prompt"), 110),
        "key": norm(ev.get("prompt"))[:40], "ts": time.time()})
    save(sid, st)


INTERRUPT = "[Request interrupted by user"


def h_stop(ev, st, sid):
    turns = st.get("turns") or []
    if not turns:
        return
    got, reply = since_stop(ev.get("transcript_path"), st)
    # 이번 턴에 전달되지 않은 요청은 대기열에 남아 다음 턴이 된다.
    # transcript 를 읽을 수 없으면 전부 닫는다 (미완료로 남는 것보다 낫다).
    closing, left = [], []
    for t in turns:
        at = 0 if got is None else next((i for i, g in enumerate(got) if t["key"] in g), None)
        (left if at is None else closing).append((at, t))
    closing.sort(key=lambda x: x[0])
    now, titled = time.time(), False
    for n, (at, t) in enumerate(closing):
        upto = closing[n + 1][0] if n + 1 < len(closing) else len(got or [])
        if got and any(g.startswith(INTERRUPT) for g in got[at + 1:upto]):
            text = "중단됨"
        elif titled:
            text = "↳ 앞 요청과 함께 처리"
        elif reply:
            text, titled = html_label(headline(reply), 100), True
        else:
            text = None                          # 응답을 못 찾으면 요청 앞부분을 쓴다
        name = f"{t['hm']} {text}" if text and t.get("hm") else t["label"]
        mins = (now - max(t["ts"], st.get("last_stop", 0))) / 60
        edit(t["id"], name=f"{name} <i>· {mins:.0f}분</i>")
        done(t["id"])
    st.update(turns=[t for _, t in left], last_stop=now)
    save(sid, st)


def h_end(ev, st, sid):
    nid = st.get("session_node")
    if not nid:
        return
    for t in st.get("turns") or []:
        try: done(t["id"])
        except Exception: pass
    mins = (time.time() - st.get("started", time.time())) / 60
    node(nid, f"⏹ 종료 · {ev.get('reason','?')} · {mins:.0f}분")
    done(nid)
    spath(sid).unlink(missing_ok=True)


def h_note(text, st, sid):
    parent = ((st.get("turns") or [{}])[-1].get("id")) or st.get("session_node")
    if not parent:
        return
    node(parent, "▸ " + label(text, 300),
         note=scrub(text, 2000) if len(text) > 300 else None)


def h_link(st, sid):
    nid = st.get("session_node")
    print(f"https://workflowy.com/#/{short(nid)}" if nid else "기록 중인 세션 없음")

# ----------------------------------------------------------------- 설치


def _ours(block):
    return any("wf.py" in h.get("command", "") for h in block.get("hooks", []))


def _write_settings(remove=False):
    p = CLAUDE / "settings.json"
    cur = {}
    if p.exists():
        try:
            cur = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            print(f"  !! {p} 가 올바른 JSON이 아닙니다. 건드리지 않고 중단합니다.")
            return False
        shutil.copy(p, p.with_suffix(".json.bak"))
    hooks = cur.get("hooks", {})
    for ev in set(hooks) | set(HOOKS):           # 재설치 대비: 기존 wf 블록 먼저 제거 (폐지된 이벤트 포함)
        kept = [b for b in hooks.get(ev, []) if not _ours(b)]
        if not remove:
            kept += HOOKS.get(ev, [])
        if kept: hooks[ev] = kept
        elif ev in hooks: del hooks[ev]
    if hooks: cur["hooks"] = hooks
    elif "hooks" in cur: del cur["hooks"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def _write_memo(remove=False):
    p = CLAUDE / "CLAUDE.md"
    cur = p.read_text(encoding="utf-8") if p.exists() else ""
    if MARK in cur:                              # 기존 블록 제거 (idempotent)
        a, _, rest = cur.partition(MARK)
        _, _, b = rest.partition(MARK)
        cur = (a.rstrip() + "\n" + b.lstrip()).strip()
    if not remove:
        cur = (cur + "\n" + MEMO).strip() + "\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cur + ("\n" if cur and not cur.endswith("\n") else ""), encoding="utf-8")


def do_install(argv):
    key  = _arg(argv, "--key")  or os.environ.get("WORKFLOWY_API_KEY")
    root = _arg(argv, "--root") or os.environ.get("WORKFLOWY_ROOT_ID")
    if (not key or not root) and sys.stdin.isatty():
        key  = key  or input("Workflowy API Key: ").strip()
        root = root or input("root node short ID: ").strip()
    if not key or not root:
        print("사용법: python3 wf.py install --key <API_KEY> --root <SHORT_ID>")
        return 1

    CFG.mkdir(parents=True, exist_ok=True)
    (CFG / "api_key").write_text(key, encoding="utf-8")
    (CFG / "root_id").write_text(root, encoding="utf-8")
    os.chmod(CFG, 0o700)
    for f in ("api_key", "root_id"):
        os.chmod(CFG / f, 0o600)
    print(f"  ok  자격 증명  -> {CFG}/ (0600)")

    src = pathlib.Path(__file__).resolve()
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if src != DEST.resolve():
        shutil.copy(src, DEST)
    os.chmod(DEST, 0o755)
    print(f"  ok  스크립트    -> {DEST}")

    if not _write_settings():
        return 1
    print(f"  ok  훅 5개      -> {CLAUDE}/settings.json  (기존 설정 보존, .bak 생성)")

    sk = CLAUDE / "skills" / "session-log" / "SKILL.md"
    sk.parent.mkdir(parents=True, exist_ok=True)
    sk.write_text(SKILL, encoding="utf-8")
    print(f"  ok  스킬        -> {sk}")

    _write_memo()
    print(f"  ok  상시 지시   -> {CLAUDE}/CLAUDE.md")

    print("\n연결 확인 중...")
    return do_doctor()


def do_uninstall():
    _write_settings(remove=True)
    _write_memo(remove=True)
    shutil.rmtree(CLAUDE / "skills" / "session-log", ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    DEST.unlink(missing_ok=True)
    print("제거 완료. 자격 증명(~/.config/workflowy)은 남겨뒀습니다.")
    return 0


def do_doctor():
    ok = True
    key, root = conf("WORKFLOWY_API_KEY", "api_key"), conf("WORKFLOWY_ROOT_ID", "root_id")
    print(f"  {'ok ' if key else 'FAIL'} API key      {'설정됨' if key else '없음'}")
    print(f"  {'ok ' if root else 'FAIL'} root id      {root or '없음'}")
    ok &= bool(key and root)

    if PLUGIN:
        print(f"  ok  설치 형태    플러그인 ({os.environ['CLAUDE_PLUGIN_ROOT']})")
        print(f"  ok  상태 저장    {STATE}")
        return _doctor_api(key, root, ok)

    for lbl, p in [("스크립트", DEST), ("설정", CLAUDE / "settings.json"),
                   ("스킬", CLAUDE / "skills" / "session-log" / "SKILL.md"),
                   ("상시 지시", CLAUDE / "CLAUDE.md")]:
        e = p.exists()
        print(f"  {'ok ' if e else 'FAIL'} {lbl:10s} {p if e else '없음'}")
        ok &= e

    try:
        n = json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8")).get("hooks", {})
        have = [e for e in HOOKS if any(_ours(b) for b in n.get(e, []))]
        good = len(have) == len(HOOKS)
        print(f"  {'ok ' if good else 'FAIL'} 훅 등록      {len(have)}/{len(HOOKS)}  {have}")
        ok &= good
    except Exception as e:
        print(f"  FAIL 훅 등록      {e}"); ok = False

    return _doctor_api(key, root, ok)


def _doctor_api(key, root, ok):
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
        print(f"\n  주의: {ERRLOG} 에 기록된 오류가 있습니다 (마지막 3줄)")
        for ln in ERRLOG.read_text(encoding="utf-8").strip().split("\n")[-3:]:
            print("        " + ln)

    print("\n" + ("전부 정상. Claude Code를 새로 시작하면 기록이 시작됩니다."
                  if ok else "문제가 있습니다. 위의 FAIL 항목을 확인하세요."))
    return 0 if ok else 1


def _arg(argv, name):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    for a in argv:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None

# ----------------------------------------------------------------- 진입점


def main():
    # Windows 에서 파이프로 연결된 표준 입출력은 로캘 인코딩(cp949 등)을 쓴다.
    # Claude Code 는 훅과 UTF-8 로 주고받으므로 명시적으로 맞춘다.
    # 입력의 깨진 바이트는 서로게이트가 되어 API 가 500 을 내므로 치환한다.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    mode = sys.argv[1] if len(sys.argv) > 1 else "doctor"

    if mode == "install":   sys.exit(do_install(sys.argv[2:]))
    if mode == "uninstall": sys.exit(do_uninstall())
    if mode == "doctor":    sys.exit(do_doctor())

    ev, text = {}, ""
    if mode in ("note", "close", "link"):
        sid  = sys.argv[2] if len(sys.argv) > 2 else ""
        text = " ".join(sys.argv[3:])
    else:
        try: ev = json.loads(sys.stdin.read() or "{}")
        except Exception: ev = {}
        sid = ev.get("session_id", "")

    st = load(sid)
    if "turn_node" in st:                        # 1.0.x 상태 파일: 열린 턴을 새 형식으로 옮긴다
        st.setdefault("turns", []).append({
            "id": st.pop("turn_node"), "label": st.pop("turn_label", "턴"),
            "key": "", "ts": st.pop("turn_started", time.time())})
    try:
        if   mode == "session-start": h_start(ev, st, sid)
        elif mode == "prompt":        h_prompt(ev, st, sid)
        elif mode == "stop":          h_stop(ev, st, sid)
        elif mode == "session-end":   h_end(ev, st, sid)
        elif mode == "note":          h_note(text, st, sid)
        elif mode == "close":         h_end({"reason": "manual"}, st, sid)
        elif mode == "link":          h_link(st, sid)
    except Exception as e:
        ERRLOG.parent.mkdir(parents=True, exist_ok=True)
        with ERRLOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%F %T} [{mode}] {type(e).__name__}: {e}\n")
    sys.exit(0)   # 훅은 무슨 일이 있어도 0. exit 2는 세션을 차단한다.


if __name__ == "__main__":
    main()
