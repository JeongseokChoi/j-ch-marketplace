"""
wfapi.py - Workflowy API 공통 부분. MCP 서버(mcp.py)와 훅(wf.py)이 함께 쓴다.

API key 는 플러그인 userConfig 에서 온다. MCP 서버에는 plugin.json 의 env 로 WORKFLOWY_API_KEY 를,
훅에는 Claude Code 가 CLAUDE_PLUGIN_OPTION_API_KEY 를 넘긴다. Claude 의 Bash 에는 둘 다 없다.
"""
import json, os, re, time, urllib.request, urllib.error

API = "https://workflowy.com/api/v1"

SECRET = re.compile(
    r"sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}|(?i:bearer)\s+[A-Za-z0-9._\-]{20,}"
    r"|(?i:api[_\-]?key|token|password|secret)\s*[=:]\s*\S{8,}")

# name 필드는 마크다운을 파싱한다. 백슬래시 이스케이프가 통하는 문자들.
MD = re.compile(r"([*`\[\]])")

# 화면에서 확인한 layoutMode. API 는 아무 문자열이나 저장하고, 모르는 값은 bullet 으로 그린다.
# code-block 은 layoutMode 로 주면 첫 줄만 블록이 되고 나머지는 note 로 빠진다.
# name 을 ``` 로 감싸면 여러 줄이 한 블록에 들어가고 layoutMode 도 code-block 이 된다.
TYPES = ("bullets", "todo", "h1", "h2", "h3", "p", "quote-block", "code")

SHORT = re.compile(r"(?:#/)?([0-9a-f]{12})/?$")   # URL 끝, short id, 전체 UUID 모두 끝 12자리가 같다


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


def label(s, n=400):
    """사람이 쓴 게 아닌 문자열(도구 description 등)의 마크다운 파싱을 중화한다."""
    return MD.sub(r"\\\1", scrub(s, n)).replace("~~", "∼∼")


def short(nid):
    m = SHORT.search(str(nid or "").strip().lower())
    return m.group(1) if m else None


def url(nid): return f"https://workflowy.com/#/{short(nid)}"


def call(method, path, body=None, tries=3):
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
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(1.5 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(1.0); continue
            raise


def get(nid):
    """short id 로도 읽힌다. 돌려주는 node 의 id 는 전체 UUID."""
    return call("GET", f"/nodes/{nid}")["node"]


def create(parent, name, type="bullets", note=None):
    """노드를 맨 아래에 만들고 전체 UUID 를 돌려준다. 순서가 곧 만든 순서가 되도록 position 은 받지 않는다."""
    if type not in TYPES:
        raise ValueError(f"지원하지 않는 type: {type} (가능: {', '.join(TYPES)})")
    name = str(name or "").replace("\r\n", "\n").strip("\n")
    note = str(note or "").replace("\r\n", "\n").strip("\n")
    if not name.strip():
        raise ValueError("name 이 비어 있음")
    b = {"parent_id": parent, "position": "bottom"}
    if type == "code":
        b["name"] = "```\n" + scrub(name.replace("```", "'''"), 8000, lines=True) + "\n```"
    else:
        # 여러 줄 name 은 Workflowy 가 첫 줄만 name 으로 두고 나머지를 note 로 옮긴다. 그 동작을 직접 한다.
        first, _, rest = name.partition("\n")
        name, note = first.strip(), "\n\n".join(x for x in (rest.strip("\n"), note) if x)
        b["name"] = scrub(name, 1000)
        b["layoutMode"] = type
    if note:
        b["note"] = scrub(note, 8000, lines=True)
    return call("POST", "/nodes", b)["item_id"]


def complete(nid):
    """완료 처리는 전체 UUID 만 받는다 (short id 는 404). short id 면 먼저 읽어서 바꾼다."""
    full = nid if "-" in str(nid) else get(nid)["id"]
    call("POST", f"/nodes/{full}/complete")
    return full
