"""
wfapi.py - Workflowy API 공통 부분. MCP 서버(mcp.py)와 훅(wf.py)이 함께 쓴다.

API key 는 플러그인 userConfig 에서 온다. MCP 서버에는 plugin.json 의 env 로 WORKFLOWY_API_KEY 를,
훅에는 Claude Code 가 CLAUDE_PLUGIN_OPTION_API_KEY 를 넘긴다. Claude 의 Bash 에는 둘 다 없다.
"""
import email.utils, html, json, os, queue, re, threading, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone

API = "https://workflowy.com/api/v1"

SECRET = re.compile(
    r"sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}|(?i:bearer)\s+[A-Za-z0-9._\-]{20,}"
    r"|(?i:api[_\-]?key|token|password|secret)\s*[=:]\s*\S{8,}")

# name 필드는 마크다운을 파싱한다. 백슬래시로 막으면 서식은 안 되지만 백슬래시가 화면에 남는다 (화면에서 확인).
# 서식이 되는 글자를 비슷한 글자로 바꾼다: * ` 는 모두, ~~ 와 링크 모양 ]( 는 그 짝만, 줄머리의 제목·todo 표시.
MD   = str.maketrans({"*": "∗", "`": "ˋ"})
LEAD = re.compile(r"^(?:#{1,3} |- \[[ xX]\] )")

# name·note 는 HTML 로도 해석된다. 꺾쇠는 날것이든 엔티티(&lt;)든 백슬래시를 붙이든 태그가 되어
# 서식이 붙거나 이름이 통째로 사라진다 (코드 블록 안도 같다. 화면에서 확인). 비슷한 글자로 바꿔 보낸다.
ANGLE  = str.maketrans({"<": "‹", ">": "›"})
ENTITY = re.compile(r"&(?=#?\w+;)")

# 읽어 온 name 은 HTML 태그로 온다. 태그를 걷어 평문으로 둔다 (링크는 주소를 남긴다).
LINK = re.compile(r'<a\s[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
TAG  = re.compile(r"<[^>]*>")

# 화면에서 확인한 layoutMode. API 는 아무 문자열이나 저장하고, 모르는 값은 bullet 으로 그린다.
# code-block 은 layoutMode 로 주면 첫 줄만 블록이 되고 나머지는 note 로 빠진다.
# name 을 ``` 로 감싸면 여러 줄이 한 블록에 들어가고 layoutMode 도 code-block 이 된다.
# 제목(h1·h2·h3)은 쓰지 않는다. 요청(request)만 서버가 굵은 bullets 로 만들고, 나머지 name 은 평문이다.
TYPES = ("bullets", "todo", "p", "quote-block", "code")

# todo 를 닫는 방식과, 그 todo 아래에 쓰는 이유 노드의 머리말. hold 만 체크하지 않는다.
OUTCOMES = {"done": "결과", "cancel": "✕ 취소", "replace": "↪ 변경", "hold": "⏸ 보류"}

SHORT = re.compile(r"(?:#/)?([0-9a-f]{12})/?$")   # URL 끝, short id, 전체 UUID 모두 끝 12자리가 같다

TOOL = "▹ "                                        # 훅이 도구 실행마다 붙이는 노드의 머리말

# 트리를 읽을 때 자식을 읽지 않는 노드: 훅이 붙인 ▹ 도구 실행, close 가 쓴 이유 노드, 단락·인용·코드.
# 우리가 그 아래에 쓰지 않으므로 호출을 아낀다.
LEAF_NAME  = re.compile(r"\s*(?:" + re.escape(TOOL) + "|(?:" + "|".join(map(re.escape, OUTCOMES.values())) + r"): )")
LEAF_TYPES = ("p", "quote-block", "code-block")

RETRIED, _lock = {}, threading.Lock()              # 재시도한 HTTP 상태 코드별 횟수 (doctor 가 병렬 읽기를 볼 때 쓴다)

# call 이 짧게(1.5초, 3초) 기다렸다 다시 부르는 HTTP 상태 코드. 훅·MCP 는 응답이 늦어지면 안 되므로 이것만 쓴다.
RETRY = (429, 500, 502, 503, 504)
# 백그라운드 sync 가 요청 한도(HTTP 429)에 걸렸을 때 기다리는 시간(초). 성공 없이 429 가 이어지면 차례로 늘리고,
# 다 기다리고도 429 면 포기한다 (Gate). 응답에 Retry-After 가 있으면 그 값을 따르되 RETRY_CAP 초를 넘지 않는다.
BACKOFF, RETRY_CAP = (10, 20, 30, 30, 60, 60), 120
IDLE = 120                                         # 백그라운드 sync 가 결과 없이 이만큼(초) 지나면 멈춘다 (한도로 멈춘 시간은 빼고)


def conf(env, key):
    """우선순위: 환경변수 -> 플러그인 userConfig (CLAUDE_PLUGIN_OPTION_<KEY>)."""
    v = os.environ.get(env)
    if v and not v.startswith("${"):              # 치환되지 않은 ${user_config.*} 는 없는 것으로 본다
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


def safe(s):
    """HTML 로 해석될 글자를 막는다: 꺾쇠는 ‹ ›, 엔티티처럼 보이는 & 는 ＆. scrub 의 <<redacted>> 도 여기서 막힌다."""
    return ENTITY.sub("＆", str(s or "")).translate(ANGLE)


def plain(s):
    """name 이 적은 글자 그대로 보이게: HTML·마크다운으로 해석될 글자를 비슷한 글자로 바꾼다."""
    s = safe(s).translate(MD).replace("~~", "∼∼").replace("](", "］(")
    return LEAD.sub(lambda m: m.group(0).replace("#", "＃").replace("- [", "‐ ["), s)


def label(s, n=400):
    """도구 description 같은 문자열을 한 줄 평문 제목으로."""
    return plain(scrub(s, n))


def unhtml(s):
    """API 가 돌려준 name 을 평문으로: 태그를 걷고, 링크는 '글자 (주소)' 로. 엔티티는 태그를 걷은 뒤에 푼다.
    서식은 되살리지 않는다 — 글자 그대로의 ** 와 굵게를 구별할 수 없게 되기 때문이다."""
    def link(m):
        t = TAG.sub("", m.group(2))
        return t if html.unescape(t) == html.unescape(m.group(1)) else f"{t} ({m.group(1)})"
    return html.unescape(TAG.sub("", LINK.sub(link, str(s or ""))))


def short(nid):
    m = SHORT.search(str(nid or "").strip().lower())
    return m.group(1) if m else None


def url(nid): return f"https://workflowy.com/#/{short(nid)}"


def call(method, path, body=None, tries=3, retry=RETRY):
    """retry 에 든 HTTP 상태 코드와 연결 오류는 짧게 기다렸다 다시 부른다 (모두 tries 번까지)."""
    key = conf("WORKFLOWY_API_KEY", "api_key")
    if not key:
        raise RuntimeError("Workflowy API key 없음 (플러그인 설정의 api_key 를 확인하세요)")
    data = json.dumps(body).encode() if body is not None else None
    for i in range(tries):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in retry and i < tries - 1:
                with _lock:
                    RETRIED[e.code] = RETRIED.get(e.code, 0) + 1
                time.sleep(1.5 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(1.0); continue
            raise


def get(nid):
    """short id 로도 읽힌다. 돌려주는 node 의 id 는 전체 UUID."""
    return call("GET", f"/nodes/{nid}")["node"]


def children(parent, retry=RETRY):
    """parent 의 직계 자식. API 는 순서 없이 주므로 priority 순으로 정렬한다."""
    q = urllib.parse.urlencode({"parent_id": parent})
    return sorted(call("GET", f"/nodes?{q}", retry=retry).get("nodes") or [], key=lambda n: n.get("priority") or 0)


class Gate:
    """한 프로세스의 작업자들이 함께 쓰는 멈춤. 누구든 429 를 받으면 멈춤이 끝나는 시각을 정하고,
    모든 작업자가 다음 호출 전에 그때까지 기다린다 — 한도에 걸린 동안 호출을 계속 보내지 않는다.
    기다리는 시간은 Retry-After, 없으면 BACKOFF 를 차례로 쓴다. 단계는 sync 전체에서 센다: 멈춤이 풀린 뒤 성공 없이
    또 429 면 한 단계 올리고, 한 번이라도 성공하면 처음으로 돌린다. BACKOFF 를 다 기다리고도 성공 없이 429 면
    포기한다(spent) — 그 뒤의 호출은 부르지 않고 같은 429 로 실패한다."""

    def __init__(self):
        self.until, self.waits, self.level, self.strained, self.spent = 0.0, 0, 0, False, None
        self._lock = threading.Lock()

    def hold(self, e):
        """429(e)를 받았다. 새로 멈추면 True, 이미 멈춘 동안 받은 것이거나 포기했으면 False."""
        with self._lock:
            now, ra = time.time(), retry_after(e)
            if now < self.until:                   # 이미 멈춘 동안 받은 429 (그 전에 보낸 호출) 는 같은 멈춤으로 본다
                if ra is not None:
                    self.until = max(self.until, now + ra)
                return False
            if self.strained:                      # 지난 멈춤이 풀린 뒤 한 번도 성공하지 못했다
                self.level += 1
            if self.level >= len(BACKOFF):
                self.spent = e
                return False
            self.waits += 1
            self.strained = True
            self.until = now + (ra if ra is not None else BACKOFF[self.level])
            return True

    def ok(self):
        with self._lock:
            self.level, self.strained = 0, False

    def paused(self):
        return time.time() < self.until

    def wait(self):
        while True:
            with self._lock:
                left = self.until - time.time()
            if left <= 0:
                return
            time.sleep(left)


def retry_after(e):
    """429 응답의 Retry-After(초 또는 HTTP 날짜)를 초로. 없거나 읽지 못하면 None. 1초에서 RETRY_CAP 초 사이로 맞춘다."""
    v = (e.headers.get("Retry-After") if getattr(e, "headers", None) else None) or ""
    try:
        sec = float(v)
    except ValueError:
        try:
            sec = (email.utils.parsedate_to_datetime(v) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, IndexError):
            return None
    return max(1.0, min(sec, RETRY_CAP))


def patient(fn, gate, waited=None):
    """백그라운드 sync 의 호출. 429 를 받으면 gate 로 모든 작업자를 멈추고 기다렸다 다시 부른다.
    fn 은 429 를 스스로 재시도하지 않아야 한다 (retry 에서 429 를 뺀 호출). 다른 오류는 그대로 올린다.
    한 호출은 len(BACKOFF)+1 번까지 부른다 — 다른 노드는 성공하는데 이 노드만 계속 429 일 때의 끝.
    waited 는 새로 멈출 때마다 불린다 (진행 표시를 바로 갱신할 때)."""
    for i in range(len(BACKOFF) + 1):
        gate.wait()
        if gate.spent:
            raise gate.spent
        try:
            got = fn()
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == len(BACKOFF):
                raise
            if gate.hold(e) and waited:
                waited()
            continue
        gate.ok()
        return got


def why(e):
    """실패 이유를 짧게: HTTP 상태 코드, 아니면 예외 이름."""
    return f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__


def leaf(n):
    """트리를 읽을 때 자식을 읽지 않는 노드인가 (LEAF_NAME, LEAF_TYPES)."""
    return (n.get("data") or {}).get("layoutMode") in LEAF_TYPES or bool(LEAF_NAME.match(n.get("name") or ""))


def tool_run(n):
    """API 가 돌려준 노드가 훅이 붙인 ▹ 도구 실행인가. 이어받는 트리에 넣지 않고, 읽은 노드 수에도 세지 않는다."""
    return unhtml(n.get("name")).lstrip().startswith(TOOL)


def subtree(root, limit=20.0, workers=8, progress=None):
    """root 아래 트리를 읽는다. root 의 자식(요청 목록)을 먼저 읽고, 최신 요청부터 그 하위를 병렬로 읽는다.
    limit 초(시간 한도)가 지나면 멈추고 읽은 만큼 돌려준다. 훅 timeout 에 걸려 통째로 잃지 않기 위해서다.
    limit 이 None 이면 끝까지 읽는다 (백그라운드 sync). 이때만 요청 한도(429)에 오래 기다린다: 모든 작업자가 함께
    멈추고 BACKOFF 만큼 기다렸다 다시 부른다 (patient). 훅·doctor(limit 있음)는 call 의 짧은 재시도 그대로다.
    progress(노드 수, 호출 수, 한도 대기 횟수) 는 2초에 한 번쯤 불린다.
    그 노드 수는 ▹ 도구 실행을 뺀 수다 — 이어받는 트리(from_api)가 ▹ 를 넣지 않으므로, sync 가 끝난 뒤 알리는 수와 맞춘다.
    root 의 자식을 읽지 못하면 예외를 그대로 올린다 (호출한 쪽이 로컬 기록으로 대체한다).
    돌려주는 값: nodes(API 노드. 트리 순서가 아니다), missing(자식을 읽지 못한 노드 id), times(성공한 호출별 초),
    errors(실패한 호출 수), reasons(실패 이유별 횟수), waits(한도로 멈춘 횟수), seconds(전체 초).
    스레드는 daemon 으로 직접 띄운다. ThreadPoolExecutor 는 프로세스가 끝날 때 한도를 넘긴 호출까지 기다린다."""
    t0 = time.time()
    end = None if limit is None else t0 + limit
    gate = Gate()
    if limit is None:                            # 429 는 call 이 짧게 재시도하지 않고 patient 가 기다린다
        slow = tuple(c for c in RETRY if c != 429)
        def get(nid, waited=None): return patient(lambda: children(nid, retry=slow), gate, waited)
    else:
        def get(nid, waited=None): return children(nid)
    top = get(root, lambda: progress and progress(0, 0, gate.waits))
    nodes, times, failed, reasons = list(top), [], [], {}
    kept = sum(1 for n in top if not tool_run(n))     # progress 에 알리는 노드 수 (▹ 제외)
    todo, done, stop = queue.PriorityQueue(), queue.Queue(), threading.Event()
    waiting = set()                              # 넣었지만 아직 결과를 받지 못한 노드

    def put(rank, depth, n):
        waiting.add(n["id"])
        todo.put((rank, depth, n["id"]))

    def work():
        while not stop.is_set():
            rank, depth, nid = todo.get()
            if stop.is_set():
                return
            s = time.time()
            try:
                got = get(nid)
            except Exception as e:
                got = e
            done.put((rank, depth, nid, got, time.time() - s))

    for rank, n in enumerate(reversed(top)):     # rank 0 이 최신 요청. 같은 rank 의 하위가 먼저 읽힌다
        if not leaf(n):
            put(rank, 0, n)
    for _ in range(workers if waiting else 0):
        threading.Thread(target=work, daemon=True).start()
    shown = last = time.time()

    def report():
        nonlocal shown
        if progress and time.time() - shown >= 2:
            shown = time.time()
            progress(kept, len(times) + len(failed) + 1, gate.waits)

    while waiting and (end is None or time.time() < end):
        try:
            rank, depth, nid, got, sec = done.get(timeout=2 if end is None else end - time.time())
        except ValueError:                       # 그 사이 한도가 지나 timeout 이 음수
            break
        except queue.Empty:
            if end is not None:
                break
            # 한도가 없어도 결과가 IDLE 초 동안 하나도 안 오면 멈춘다 (호출 하나는 재시도까지 30초 안쪽이다).
            # 429 로 다 같이 멈춘 동안은 세지 않는다
            if gate.paused():
                last = time.time()
            elif time.time() - last >= IDLE:
                break
            report()
            continue
        last = time.time()
        waiting.discard(nid)
        if isinstance(got, Exception):
            failed.append(nid)
            reasons[why(got)] = reasons.get(why(got), 0) + 1
        else:
            times.append(sec)
            nodes += got
            kept += sum(1 for c in got if not tool_run(c))
            for c in got:
                if not leaf(c):
                    put(rank, depth + 1, c)
        report()                                 # 방금 받은 자식까지 센 뒤에 알린다
    stop.set()
    return {"nodes": nodes, "missing": failed + sorted(waiting), "times": times, "errors": len(failed),
            "reasons": reasons, "waits": gate.waits, "seconds": time.time() - t0}


def request_title(name):
    """요청 제목의 글자. 굵게는 서버가 전체에 붙이므로 Claude 가 적은 ** 는 걷어 낸다."""
    return str(name or "").replace("**", "").strip()


def request_note(note):
    """요청 노드의 note: 첫 줄에 날짜·시각. 세션의 경계를 알아보는 표시다."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return stamp + ("\n" + note if note else "")


def create(parent, name, type="bullets", note=None, request=False):
    """노드를 맨 아래에 만들고 전체 UUID 를 돌려준다. 순서가 곧 만든 순서가 되도록 position 은 받지 않는다.
    name 은 평문으로 보낸다 (plain). request 면 요청 노드로 만든다: 제목은 굵게, note 첫 줄에 날짜·시각."""
    if type not in TYPES:
        raise ValueError(f"지원하지 않는 type: {type} (가능: {', '.join(TYPES)})")
    name = str(name or "").replace("\r\n", "\n").strip("\n")
    note = str(note or "").replace("\r\n", "\n").strip("\n")
    if not name.strip():
        raise ValueError("name 이 비어 있음")
    b = {"parent_id": parent, "position": "bottom"}
    if type == "code":
        b["name"] = "```\n" + safe(scrub(name.replace("```", "'''"), 8000, lines=True)) + "\n```"
    else:
        # 여러 줄 name 은 Workflowy 가 첫 줄만 name 으로 두고 나머지를 note 로 옮긴다. 그 동작을 직접 한다.
        first, _, rest = name.partition("\n")
        name, note = scrub(first.strip(), 1000), "\n\n".join(x for x in (rest.strip("\n"), note) if x)
        if request:
            t, note = request_title(name), request_note(note)
            if not t:
                raise ValueError("name 이 비어 있음")
            b["name"] = f"**{plain(t)}**"
        else:
            b["name"] = plain(name)
        b["layoutMode"] = type
    if note:
        b["note"] = safe(scrub(note, 8000, lines=True))
    return call("POST", "/nodes", b)["item_id"]


def complete(nid):
    """완료 처리는 전체 UUID 만 받는다 (short id 는 404). short id 면 먼저 읽어서 바꾼다."""
    full = nid if "-" in str(nid) else get(nid)["id"]
    call("POST", f"/nodes/{full}/complete")
    return full


def ids_of(v):
    """close 의 ids. 배열이 정상이지만 문자열 하나도 받는다."""
    return [v] if isinstance(v, str) else [x for x in (v or []) if x]


def close_note(outcome, reason):
    """close 가 todo 아래에 쓰는 이유 노드의 name."""
    return f"{OUTCOMES[outcome]}: {reason}"
