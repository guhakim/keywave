"""KEYWAVE 서버: 정적 파일 + 키워드 분석 API.

- 검색량·연관 키워드·경쟁: 네이버 검색광고 API
- 블로그 발행량·12개월 그래프: NAVER API HUB (예전 개발자센터 키도 사용 가능)
- 사이트별 연관 키워드: 각 검색 사이트의 공개 자동완성 주소
- 급상승 검색어: 구글 트렌드 RSS
열쇠는 같은 폴더의 .env 파일에 저장합니다 (/setup.html 에서 붙여넣기). 열쇠가 없는 칸은 비워 둡니다.
"""
import base64, datetime, hashlib, hmac, json, os, re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))


ENV_PATH = os.path.join(ROOT, ".env")
ENV_NAMES = ["NAVER_AD_API_KEY", "NAVER_AD_SECRET_KEY", "NAVER_AD_CUSTOMER_ID",
             "NAVER_HUB_CLIENT_ID", "NAVER_HUB_CLIENT_SECRET", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET",
             "ANTHROPIC_API_KEY", "GEMINI_API_KEY"]


def read_env():
    env = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def write_env(updates):
    """.env의 해당 줄만 바꾸고 주석·다른 줄은 그대로 둡니다. 나만 읽을 수 있게(600) 저장합니다."""
    lines = open(ENV_PATH, encoding="utf-8").read().splitlines() if os.path.exists(ENV_PATH) else []
    done = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and "=" in line and k in updates:
            lines[i] = f"{k}={updates[k]}"
            done.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)


def configure():
    """.env(없으면 환경 변수)에서 열쇠를 읽어 전역 설정을 만듭니다. 열쇠를 저장하면 다시 불립니다."""
    global AD_KEY, AD_SECRET, AD_CUSTOMER, OPEN_ID, OPEN_SECRET, HUB_ID, HUB_SECRET, OPEN_API, AI_KEY, GEMINI_KEY
    env = {k: os.getenv(k, "") for k in ENV_NAMES}
    env.update({k: v for k, v in read_env().items() if v})
    AD_KEY = env["NAVER_AD_API_KEY"]            # 검색광고 API 액세스라이선스
    AD_SECRET = env["NAVER_AD_SECRET_KEY"]      # 검색광고 API 비밀키
    AD_CUSTOMER = env["NAVER_AD_CUSTOMER_ID"]   # 검색광고 고객 ID
    OPEN_ID = env["NAVER_CLIENT_ID"]            # 네이버 개발자센터 Client ID (예전 방식)
    OPEN_SECRET = env["NAVER_CLIENT_SECRET"]    # 네이버 개발자센터 Client Secret (예전 방식)
    HUB_ID = env["NAVER_HUB_CLIENT_ID"]         # NAVER API HUB (네이버 클라우드) Client ID
    HUB_SECRET = env["NAVER_HUB_CLIENT_SECRET"] # NAVER API HUB Client Secret
    AI_KEY = env["ANTHROPIC_API_KEY"]           # Claude API 키 (AI 인사이트용, 선택, 유료)
    GEMINI_KEY = env["GEMINI_API_KEY"]          # Google Gemini API 키 (AI 인사이트용, 선택, 무료 사용량 있음)

    # 2026-07-31부터 검색·데이터랩 신규 신청은 NAVER API HUB로 옮겨졌습니다.
    # HUB 키가 있으면 HUB를, 없으면 예전 개발자센터 키를 씁니다. 응답 형식은 둘이 같습니다.
    if HUB_ID and HUB_SECRET:
        OPEN_API = {"name": "NAVER API HUB",
                    "blog": "https://naverapihub.apigw.ntruss.com/search/v1/blog",
                    "trend": "https://naverapihub.apigw.ntruss.com/search-trend/v1/search",
                    "headers": {"X-NCP-APIGW-API-KEY-ID": HUB_ID, "X-NCP-APIGW-API-KEY": HUB_SECRET}}
    elif OPEN_ID and OPEN_SECRET:
        OPEN_API = {"name": "네이버 개발자센터",
                    "blog": "https://openapi.naver.com/v1/search/blog.json",
                    "trend": "https://openapi.naver.com/v1/datalab/search",
                    "headers": {"X-Naver-Client-Id": OPEN_ID, "X-Naver-Client-Secret": OPEN_SECRET}}
    else:
        OPEN_API = None


configure()

# ---------- 인터넷에 올렸을 때(배포) ----------
# KEYWAVE_DEPLOY가 있으면 배포 모드: 모든 주소에서 접속을 받고, 열쇠는 호스팅 서비스의 환경 변수로만 넣습니다
# (화면에서 열쇠 저장 막음 — 서버 파일은 다시 배포하면 지워지고, 남이 열쇠를 바꾸면 안 되니까요).
DEPLOYED = bool(os.getenv("KEYWAVE_DEPLOY"))


def key_status():
    """어떤 열쇠가 들어 있는지만 알려줍니다 (열쇠 값은 절대 돌려주지 않음)."""
    return {"ad": bool(AD_KEY and AD_SECRET and AD_CUSTOMER), "open": OPEN_API["name"] if OPEN_API else None,
            "ai": bool(AI_KEY or GEMINI_KEY), "ai_sdk": ai_sdk_ready(),
            "ai_providers": [p for p, k in (("gemini", GEMINI_KEY), ("claude", AI_KEY)) if k], "deployed": DEPLOYED}


def save_and_test(body):
    """setup.html에서 붙여넣은 열쇠를 .env에 저장하고, 네이버에 실제로 한 번 물어봐서 결과를 알려줍니다."""
    updates = {k: str(body.get(k, "")).strip() for k in ENV_NAMES if str(body.get(k, "")).strip()}
    if not updates:
        return 400, {"error": "붙여넣은 열쇠가 없어요."}
    write_env(updates)
    configure()
    result = {"saved": sorted(updates), **key_status()}
    if result["ad"]:
        try:
            rows = keyword_tool("캠핑")
            result["ad_test"] = f"성공! 네이버 검색광고가 연관 키워드 {len(rows)}개를 보내왔어요."
        except Exception as e:
            result["ad_test"] = f"실패: {e} — 열쇠를 다시 확인해 주세요."
    if OPEN_API:
        try:
            result["open_test"] = f"성공! {OPEN_API['name']} 블로그 글 수: {blog_total('캠핑'):,}"
        except Exception as e:
            result["open_test"] = f"실패: {e} — 열쇠를 다시 확인해 주세요."
    if AI_KEY and "ANTHROPIC_API_KEY" in updates:
        try:
            import anthropic
            anthropic.Anthropic(api_key=AI_KEY).models.retrieve(AI_MODEL)
            result["ai_test"] = f"성공! Claude API 열쇠가 맞아요 ({AI_MODEL} 사용 가능)."
        except ImportError:
            result["ai_test"] = "실패: anthropic 패키지가 없어요 — 'KEYWAVE 켜기'로 서버를 켜 주세요."
        except Exception as e:
            result["ai_test"] = f"실패: {ai_friendly(e)}"
    if GEMINI_KEY and "GEMINI_API_KEY" in updates:
        try:  # 모델 정보만 확인 (사용량을 쓰지 않음)
            http_json(f"{GEMINI_BASE}/models/{GEMINI_MODEL}", {"x-goog-api-key": GEMINI_KEY})
            result["gemini_test"] = f"성공! Gemini 열쇠가 맞아요 ({GEMINI_MODEL} 사용 가능)."
        except Exception as e:
            result["gemini_test"] = f"실패: {gemini_friendly(e)}"
    return 200, result


def http_json(url, headers, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def to_int(v):
    # 검색광고 API는 검색량이 10 미만이면 "< 10" 문자열을 돌려줍니다. 숫자를 지어내지 않고 "<10" 그대로 넘깁니다.
    return "<10" if isinstance(v, str) else int(v or 0)


def keyword_tool(keyword):
    """keyword에 쉼표로 최대 5개까지 넣을 수 있습니다 (네이버 검색광고 규칙)."""
    ts = str(int(time.time() * 1000))
    path = "/keywordstool"
    sig = base64.b64encode(hmac.new(AD_SECRET.encode(), f"{ts}.GET.{path}".encode(), hashlib.sha256).digest()).decode()
    q = urllib.parse.urlencode({"hintKeywords": keyword.replace(" ", ""), "showDetail": 1})
    res = http_json(f"https://api.searchad.naver.com{path}?{q}", {
        "X-Timestamp": ts, "X-API-KEY": AD_KEY, "X-Customer": AD_CUSTOMER, "X-Signature": sig,
    })
    return res.get("keywordList", [])


def open_api_headers():
    return {**OPEN_API["headers"], "Content-Type": "application/json"}


def blog_total(keyword):
    q = urllib.parse.quote(keyword)
    return http_json(f"{OPEN_API['blog']}?query={q}&display=1", open_api_headers()).get("total", 0)


def monthly_trend(keyword):
    end = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    start = (end.replace(day=1) - datetime.timedelta(days=335)).replace(day=1)
    res = http_json(OPEN_API["trend"], open_api_headers(), {
        "startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "month",
        "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}],
    })
    return [{"period": d["period"][:7], "ratio": d["ratio"]} for d in res["results"][0]["data"]]


AGE_CODES = {str(i) for i in range(1, 12)}  # 데이터랩 연령 코드 1(0~12세) ~ 11(60세 이상)


def trend_detail(params):
    """트렌드 자세히 보기: 비교 키워드(최대 5개)·기간·단위·기기·성별·연령을 데이터랩에 그대로 물어봅니다.
    요일별 검색 비율은 같은 조건의 '일별' 데이터를 요일마다 평균 내서 만듭니다.
    비율(ratio)은 이 요청 안에서 가장 높은 값을 100으로 한 상대값이라, 조건이 다른 결과끼리는 비교할 수 없습니다."""
    get = lambda k, d="": (params.get(k, [d])[0] or d).strip()
    words = [w.strip() for w in get("q").replace("\n", ",").split(",") if w.strip()][:5]
    if not words:
        return 400, {"error": "키워드를 입력하세요"}
    if not OPEN_API:
        return 200, {"error_note": "트렌드는 NAVER API HUB 열쇠가 있어야 나와요", "series": [], "weekday": []}
    today = datetime.date.today()
    try:
        start = datetime.date.fromisoformat(get("start", (today - datetime.timedelta(days=365)).isoformat()))
        end = datetime.date.fromisoformat(get("end", (today - datetime.timedelta(days=1)).isoformat()))
    except ValueError:
        return 400, {"error": "날짜 형식이 잘못됐어요 (예: 2025-10-01)"}
    start = max(start, datetime.date(2016, 1, 1))  # 데이터랩은 2016-01-01부터
    end = min(end, today - datetime.timedelta(days=1))
    if start > end:
        return 400, {"error": "시작 날짜가 끝 날짜보다 늦어요"}
    unit = get("unit", "month") if get("unit", "month") in ("date", "week", "month") else "month"
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": unit,
            "keywordGroups": [{"groupName": w, "keywords": [w]} for w in words]}
    if get("device") in ("pc", "mo"):
        body["device"] = get("device")
    if get("gender") in ("m", "f"):
        body["gender"] = get("gender")
    ages = [a for a in get("ages").split(",") if a in AGE_CODES]
    if ages:
        body["ages"] = ages

    out = {"start": start.isoformat(), "end": end.isoformat(), "unit": unit, "filters": {k: body.get(k) for k in ("device", "gender", "ages")}}
    try:
        res = http_json(OPEN_API["trend"], open_api_headers(), body)
        out["series"] = [{"keyword": r["title"], "data": [{"period": d["period"], "ratio": d["ratio"]} for d in r["data"]]} for r in res["results"]]
    except Exception as e:
        return 200, {**out, "error_note": f"트렌드: {friendly(e)}", "series": [], "weekday": []}

    # 요일별: 같은 조건으로 '일별'을 받아 요일마다 평균 → 일주일 합을 100%로 나눔
    try:
        res = http_json(OPEN_API["trend"], open_api_headers(), {**body, "timeUnit": "date"})
        weekday = []
        for r in res["results"]:
            sums, cnts = [0.0] * 7, [0] * 7
            for d in r["data"]:
                wd = datetime.date.fromisoformat(d["period"]).weekday()  # 0=월요일
                sums[wd] += d["ratio"]; cnts[wd] += 1
            avgs = [sums[i] / cnts[i] if cnts[i] else 0 for i in range(7)]
            total = sum(avgs) or 1
            weekday.append({"keyword": r["title"], "share": [round(a / total * 100, 1) for a in avgs], "days": sum(cnts)})
        out["weekday"] = weekday
    except Exception as e:
        out["weekday"], out["error_note"] = [], f"요일별: {friendly(e)}"
    return 200, out


def friendly(e):
    """네이버 오류 번호를 쉬운 말로 바꿉니다."""
    code = getattr(e, "code", None)
    return {400: "이 키워드는 받지 않아요 (특수문자 <, > 등이 들어 있으면 그래요)",
            401: "열쇠가 틀렸어요 — 🔑 열쇠 넣기에서 다시 저장해 주세요",
            403: "열쇠가 틀렸거나 CUSTOMER_ID가 달라요 — 🔑 열쇠 넣기에서 다시 저장해 주세요",
            429: "오늘 쓸 수 있는 횟수를 다 썼어요 — 내일 다시 해 주세요"}.get(code, f"잠시 연결이 안 돼요 ({e})")


_docs_cache = {}  # 키워드별 블로그 글 수를 6시간 기억 (HUB 무료 한도 아끼기)


def blog_totals(words, limit=40):
    """여러 키워드의 블로그 누적 글 수를 한꺼번에 가져옵니다 (최대 40개, 동시에 6개씩)."""
    words = list(dict.fromkeys(w.strip() for w in words if w.strip()))[:limit]
    out, todo = {}, []
    for w in words:
        hit = _docs_cache.get(w)
        if hit and time.time() - hit[0] < 6 * 3600:
            out[w] = hit[1]
        else:
            todo.append(w)

    def one(w):
        try:
            return w, blog_total(w)
        except Exception:
            return w, None
    if todo and OPEN_API:
        with ThreadPoolExecutor(6) as pool:
            for w, n in pool.map(one, todo):
                out[w] = n
                if n is not None:
                    _docs_cache[w] = (time.time(), n)
    return out


def is_relevant(word, keyword):
    """연관 키워드에 입력한 키워드가 들어 있는지 (띄어쓰기·대소문자 무시).
    '캠핑 의자'처럼 띄어 쓴 키워드는 낱말이 모두 들어 있으면 같은 주제로 봅니다."""
    w = word.replace(" ", "").lower()
    return all(t.lower() in w for t in keyword.split())


def analyze(keyword):
    """검색광고(검색량·연관)와 HUB(발행량·그래프)를 따로따로 물어봅니다.
    한 곳이 실패하거나 열쇠가 없어도 다른 곳 결과는 그대로 보여주고, 숫자를 지어내지 않습니다."""
    out = {"mode": "nokey", "keyword": keyword, "pc": None, "mobile": None, "docs": None,
           "comp": None, "trend": [], "related": [], "related_info": None, "errors": []}
    comp_ko = {"low": "낮음", "medium": "중간", "high": "높음"}
    comp = lambda x: comp_ko.get(str(x.get("compIdx", "")).lower(), x.get("compIdx", "-"))

    if AD_KEY and AD_SECRET and AD_CUSTOMER:
        try:
            rows = keyword_tool(keyword)
            target = keyword.replace(" ", "").lower()
            # 정확히 같은 키워드만 씁니다. 없으면 다른 키워드 숫자를 빌려오지 않고 비워 둡니다.
            main = next((x for x in rows if x["relKeyword"].replace(" ", "").lower() == target), None)
            if main:
                out.update(pc=to_int(main.get("monthlyPcQcCnt")), mobile=to_int(main.get("monthlyMobileQcCnt")), comp=comp(main))
            # 검색광고는 연관 키워드를 1,000개 넘게, 검색량과 상관없는 순서로 줍니다.
            # 광고주 기준 연관이라 '추석', '워크자켓'처럼 주제와 먼 키워드가 섞여서,
            # 입력한 키워드가 들어간 것만 남기고 검색량 순으로 위 20개를 보여줍니다.
            others = [x for x in rows if x is not main]
            rel = sorted((x for x in others if is_relevant(x["relKeyword"], keyword)),
                         key=lambda x: sum(v for v in (to_int(x["monthlyPcQcCnt"]), to_int(x["monthlyMobileQcCnt"])) if isinstance(v, int)),
                         reverse=True)
            out["related"] = [{"keyword": x["relKeyword"], "pc": to_int(x["monthlyPcQcCnt"]),
                               "mobile": to_int(x["monthlyMobileQcCnt"]), "comp": comp(x)}
                              for x in rel[:20]]
            out["related_info"] = {"total": len(others), "matched": len(rel), "shown": len(out["related"])}
            out["mode"] = "live"
        except Exception as e:
            out["errors"].append(f"검색량: {friendly(e)}")

    if OPEN_API:
        try:
            out["docs"] = blog_total(keyword)
            out["mode"] = "live"
        except Exception as e:
            out["errors"].append(f"블로그 발행량: {friendly(e)}")
        try:
            out["trend"] = monthly_trend(keyword)
        except Exception as e:
            out["errors"].append(f"12개월 그래프: {friendly(e)}")
    return out


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130 Safari/537.36"

# 각 사이트가 검색창 자동완성에 쓰는 공개 주소. 줌·카카오·페이스북 등은 공개 주소가 없어 링크만 제공합니다.
SUGGEST = {
    "google": ("https://suggestqueries.google.com/complete/search?client=firefox&hl=ko&q={q}", lambda d: d[1]),
    "naver": ("https://ac.search.naver.com/nx/ac?q={q}&st=100&r_format=json&r_enc=UTF-8&q_enc=UTF-8", lambda d: [x[0] for x in d["items"][0]]),
    "daum": ("https://suggest.search.daum.net/sushi/opensearch/pc?q={q}", lambda d: d[1]),
    "bing": ("https://api.bing.com/osjson.aspx?query={q}", lambda d: d[1]),
    "yahoo": ("https://search.yahoo.com/sugg/gossip/gossip-us-ura/?output=sugg&command={q}", lambda d: [x["key"] for x in d["gossip"]["results"]]),
    "etc": ("https://duckduckgo.com/ac/?q={q}&type=list", lambda d: d[1]),
    "youtube": ("https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&hl=ko&q={q}", lambda d: d[1]),
}


def fetch_suggest(source, keyword):
    url, parse = SUGGEST[source]
    req = urllib.request.Request(url.format(q=urllib.parse.quote(keyword)), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            words = parse(json.loads(r.read().decode("utf-8", "replace")))
    except Exception:
        return source, None
    seen, out = set(), []
    for w in words:  # 입력한 키워드 자체와 중복은 뺍니다
        w = w.strip()
        if w and w != keyword and w not in seen:
            seen.add(w)
            out.append(w)
    return source, out


def suggest_all(keyword):
    with ThreadPoolExecutor(len(SUGGEST)) as pool:
        return dict(pool.map(lambda s: fetch_suggest(s, keyword), SUGGEST))


_trend_cache = {"at": 0, "data": None}


def trending(_=None):
    """구글 트렌드 한국 급상승 검색어 (공개 RSS). 10분 동안 캐시합니다."""
    if _trend_cache["data"] and time.time() - _trend_cache["at"] < 600:
        return _trend_cache["data"]
    req = urllib.request.Request("https://trends.google.com/trending/rss?geo=KR", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as r:
        root = ET.fromstring(r.read())
    ht = "{https://trends.google.com/trending/rss}"
    items = []
    for it in root.iter("item"):
        news = it.find(f"{ht}news_item")
        items.append({
            "keyword": it.findtext("title", "").strip(),
            "traffic": it.findtext(f"{ht}approx_traffic", ""),
            "pubDate": it.findtext("pubDate", ""),
            "news": None if news is None else {
                "title": news.findtext(f"{ht}news_item_title", ""),
                "url": news.findtext(f"{ht}news_item_url", ""),
                "source": news.findtext(f"{ht}news_item_source", ""),
            },
        })
    data = {"source": "Google Trends (KR)", "updated": time.strftime("%Y-%m-%d %H:%M"), "items": items}
    _trend_cache.update(at=time.time(), data=data)
    return data


def expand(q):
    """키워드 확장: 시드 키워드(줄바꿈/쉼표로 최대 5개)의 연관 키워드를 전부 모읍니다.
    - 검색광고 연관 키워드(검색량 포함, 열쇠 필요)
    - 7개 사이트 자동완성 키워드(검색량 없음)
    같은 키워드(띄어쓰기·대소문자 무시)는 하나로 합치고, 어디서 나왔는지(sources)를 남깁니다."""
    seeds = [x.strip() for x in q.replace(",", "\n").splitlines() if x.strip()][:5]
    items, errors = {}, []

    def add(word, source, pc=None, mobile=None, comp=None, ctr=None):
        key = word.replace(" ", "").lower()
        if not key or key in {sd.replace(" ", "").lower() for sd in seeds}:
            return
        e = items.setdefault(key, {"keyword": word, "pc": None, "mobile": None, "comp": None, "ctr": None, "sources": []})
        if pc is not None:
            e.update(pc=pc, mobile=mobile, comp=comp, ctr=ctr)
        if source not in e["sources"]:
            e["sources"].append(source)

    if AD_KEY and AD_SECRET and AD_CUSTOMER:
        comp_ko = {"low": "낮음", "medium": "중간", "high": "높음"}
        try:
            for x in keyword_tool(",".join(seeds)):
                add(x["relKeyword"], "검색광고", to_int(x["monthlyPcQcCnt"]), to_int(x["monthlyMobileQcCnt"]),
                    comp_ko.get(str(x.get("compIdx", "")).lower(), x.get("compIdx", "-")),
                    # 광고 평균 클릭률(%) — 네이버 검색광고가 주는 값 그대로 (PC, 모바일)
                    {"pc": x.get("monthlyAvePcCtr"), "mobile": x.get("monthlyAveMobileCtr")})
        except Exception as e:
            errors.append(f"검색광고: {friendly(e)}")
    else:
        errors.append("검색량은 네이버 검색광고 열쇠가 있어야 나와요")

    for sd in seeds:
        for site, words in suggest_all(sd).items():
            for w in words or []:
                add(w, site)
    return {"seeds": seeds, "items": list(items.values()), "errors": errors}


# ---------- 🤖 AI 인사이트 (Claude API) ----------
# KEYWAVE가 모은 '실제 데이터'만 Claude에게 보내고, 그 안에서만 판단하게 합니다. 숫자를 새로 만들지 못하게 지시합니다.
AI_MODEL = "claude-opus-5-5"
AI_PRICE = (4.00, 20.00)   # 이 모델의 1백만 토큰당 입력/출력 가격(달러) — 비용 추정용
_ai_cache = {}             # 같은 키워드는 1시간 동안 다시 돈을 쓰지 않도록 기억

AI_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "signals", "clusters", "topics", "outline"],
    "properties": {
        "verdict": {"type": "object", "additionalProperties": False, "required": ["level", "summary"],
                    "properties": {"level": {"type": "string", "enum": ["공략 추천", "조건부 추천", "비추천", "판단 보류"]},
                                   "summary": {"type": "string"}}},
        "signals": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["name", "evidence", "meaning"],
                    "properties": {"name": {"type": "string"}, "evidence": {"type": "string"}, "meaning": {"type": "string"}}}},
        "clusters": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "required": ["name", "keywords"],
                     "properties": {"name": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}}}},
        "topics": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                   "required": ["title", "target_keyword", "why"],
                   "properties": {"title": {"type": "string"}, "target_keyword": {"type": "string"}, "why": {"type": "string"}}}},
        "outline": {"type": "object", "additionalProperties": False, "required": ["title", "reader", "sections", "checklist"],
                    "properties": {"title": {"type": "string"}, "reader": {"type": "string"},
                                   "sections": {"type": "array", "items": {"type": "string"}},
                                   "checklist": {"type": "array", "items": {"type": "string"}}}},
    },
}

AI_SYSTEM = """당신은 한국 블로그 콘텐츠 전략가입니다. 사용자가 준 [데이터] 안의 정보만 근거로 판단하세요.
규칙:
- [데이터]에 없는 숫자(검색량, 발행량, 비율, 순위 등)는 절대 만들지 마세요. 숫자를 말할 때는 [데이터]의 값을 그대로 쓰세요.
- 데이터가 비어 있거나 부족하면 그렇다고 말하고, verdict.level을 "판단 보류"로 하세요.
- "광고 경쟁"은 네이버 검색광고의 광고 입찰 경쟁이지, 블로그 상위 노출 난이도가 아닙니다. 그렇게 해석하세요.
- 데이터랩 값은 최고=100인 상대지수입니다. 실제 검색 횟수로 말하지 마세요.
- signals는 3~5개, evidence에는 [데이터]의 실제 값을 적으세요.
- clusters는 연관·자동완성 키워드를 2~4개 주제로 묶고, keywords에는 [데이터]에 있는 키워드만 넣으세요.
- 키워드끼리 경쟁을 비교할 때는 "발행량_나누기_월검색량"(블로그 글 수 ÷ 월 검색량)을 쓰세요. 낮을수록 검색에 비해 글이 적어 상대적으로 덜 붐빕니다.
- 블로그 글 수가 없는(null 이거나 목록에 없는) 키워드는 경쟁이 적다/많다고 말하지 마세요. 그런 키워드를 추천할 때는 "글 수 확인 필요"라고 적으세요.
- 메인 키워드보다 "발행량_나누기_월검색량"이 더 높은 키워드를 '덜 붐비는 대안'으로 추천하지 마세요.
- topics는 3~5개 블로그 글 주제, target_keyword는 [데이터]에 있는 키워드 중에서 고르세요.
- outline은 topics 중 가장 좋은 하나의 글 구성안입니다.
- 상위 노출을 장담하지 마세요. "노출 확률이 높다", "상위 노출된다" 대신 "덜 붐벼서 유리할 수 있다"처럼 쓰세요.
- 모든 문장은 쉬운 한국어로 짧게 쓰세요."""


def ai_sdk_ready():
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def ai_friendly(e):
    name = type(e).__name__
    return {"AuthenticationError": "Claude API 열쇠가 틀렸어요 — 🔑 열쇠 넣기에서 다시 저장해 주세요",
            "PermissionDeniedError": "이 열쇠로는 이 모델을 쓸 수 없어요",
            "RateLimitError": "요청이 너무 많아요. 잠시 뒤 다시 해 주세요",
            "APIConnectionError": "Claude API에 연결할 수 없어요 (인터넷 확인)",
            "BadRequestError": f"요청이 거절됐어요 ({getattr(e, 'message', e)})"}.get(name, f"{name}: {getattr(e, 'message', e)}")


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-3.8-flash"   # 무료 사용량이 있는 모델 (ai.google.dev 요금표 기준)
# GEMINI_MODEL이 붐비거나(503) 무료 한도를 다 썼거나(429) 답이 늦을 때 차례로 대신 쓰는 모델.
# 무료 한도는 모델마다 따로라서, 한 모델 한도를 다 써도 다른 모델은 쓸 수 있어요.
GEMINI_FALLBACKS = ["gemini-2.5-flash", "gemini-flash-lite-latest"]


def gemini_friendly(e):
    code = getattr(e, "code", None)
    body = ""
    try:
        body = e.read().decode()[:300]
    except Exception:
        pass
    if code in (400, 403) and ("API key" in body or "API_KEY" in body):
        return "Gemini 열쇠가 틀렸어요 — 🔑 열쇠 넣기에서 다시 저장해 주세요"
    if code == 503:
        return "Gemini가 지금 붐벼서 답을 못 받았어요. 몇 분 뒤 다시 해 주세요 (열쇠 문제가 아니에요)"
    if code == 429:
        return "오늘(또는 지금 1분) 무료 사용량을 다 썼어요. 잠시 뒤나 내일 다시 해 주세요"
    if code == 404:
        return f"모델({GEMINI_MODEL})을 찾을 수 없어요"
    if "timed out" in str(e):
        return "AI 답이 2분 안에 오지 않았어요. 잠시 뒤 다시 해 주세요"
    return f"Gemini 오류 ({code or e}) {body}".strip()


def gemini_schema(node):
    """Gemini responseSchema는 JSON Schema의 일부만 받아서, 안 받는 항목(additionalProperties)을 빼고 형식을 맞춥니다."""
    if isinstance(node, dict):
        return {k: gemini_schema(v) for k, v in node.items() if k != "additionalProperties"}
    if isinstance(node, list):
        return [gemini_schema(v) for v in node]
    return node


class AIError(Exception):
    """화면에 그대로 보여줄 쉬운 말 오류."""


def pick_provider(provider):
    """열쇠가 있는 쪽을 고릅니다. 둘 다 있으면 고른 쪽, 안 고르면 무료인 Gemini 먼저."""
    have = [p for p, k in (("gemini", GEMINI_KEY), ("claude", AI_KEY)) if k]
    if not have:
        raise AIError("Gemini(무료) 또는 Claude 열쇠가 있어야 써요 — 🔑 열쇠 넣기에서 넣어 주세요")
    provider = provider if provider in have else have[0]
    if provider == "claude" and not ai_sdk_ready():
        raise AIError("anthropic 패키지가 없어요 — 'KEYWAVE 켜기'로 서버를 다시 켜 주세요")
    return provider


def ask_gemini(system, data, schema):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": "[데이터]\n" + json.dumps(data, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": gemini_schema(schema)},
    }
    # 모델을 차례로 써 봅니다. 붐빔(503)은 5초 쉬고 한 번 더, 한도 초과(429)·응답 늦음·서버 오류는 바로 다음 모델로.
    # 열쇠가 틀린 것처럼 모델을 바꿔도 안 되는 오류는 바로 알려 줍니다.
    models, skipped, res = [GEMINI_MODEL] + GEMINI_FALLBACKS, [], None
    REASON = {429: "무료 한도 초과", 503: "붐빔", 500: "서버 오류", 504: "응답 늦음"}
    for i, model in enumerate(models):
        for attempt in range(2):
            try:
                res = http_json(f"{GEMINI_BASE}/models/{model}:generateContent",
                                {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}, body,
                                timeout=120 if i == len(models) - 1 else 75)  # AI 답은 오래 걸릴 수 있어요
                break
            except Exception as e:
                code = getattr(e, "code", None)
                if code == 503 and attempt == 0:
                    time.sleep(5)
                    continue
                if code in REASON or "timed out" in str(e):
                    skipped.append(f"{model} {REASON.get(code, '응답 늦음')}")
                    break
                raise AIError(gemini_friendly(e))
        if res is not None:
            break
    if res is None:
        if all("한도" in x for x in skipped):
            raise AIError(f"Gemini 무료 사용량을 모든 모델({', '.join(models)})에서 다 썼어요. 잠시 뒤나 내일 다시 해 주세요")
        raise AIError(f"Gemini 모델을 모두 써 봤지만 답을 못 받았어요 ({' · '.join(skipped)}). 몇 분 뒤 다시 해 주세요 (열쇠 문제가 아니에요)")
    cand = (res.get("candidates") or [{}])[0]
    if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
        raise AIError("AI가 이 요청에 대한 답을 거절했어요")
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    u = res.get("usageMetadata", {})
    return text, res.get("modelVersion", model), {
        "input": u.get("promptTokenCount", 0),
        "output": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
        "cost_usd": 0, "note": "무료 사용량 안에서는 0원",
        "fallback": bool(skipped) and f"{' · '.join(skipped)} → {model}로 답했어요"}


def ask_claude(system, data, schema):
    import anthropic
    client = anthropic.Anthropic(api_key=AI_KEY)
    try:
        # Claude에게 '이 데이터 안에서만' 판단해 달라고 요청 (정해진 JSON 모양으로만 답하게 함)
        resp = client.beta.messages.create(
            model=AI_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                      # 안전 정책으로 거절되면 다른 모델이 이어서 답함
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": "[데이터]\n" + json.dumps(data, ensure_ascii=False)}],
        )
    except Exception as e:
        raise AIError(ai_friendly(e))
    if resp.stop_reason == "refusal":
        raise AIError("AI가 이 요청에 대한 답을 거절했어요")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    u = resp.usage
    cost = (u.input_tokens * AI_PRICE[0] + u.output_tokens * AI_PRICE[1]) / 1_000_000
    return text, resp.model, {"input": u.input_tokens, "output": u.output_tokens, "cost_usd": round(cost, 4)}


def call_ai(provider, system, data, schema, valid):
    """Gemini 또는 Claude에 물어보고, 정해진 모양의 JSON 답만 돌려줍니다."""
    text, model, usage = (ask_gemini if provider == "gemini" else ask_claude)(system, data, schema)
    try:
        result = json.loads(text)
    except ValueError:
        raise AIError("AI 답을 읽지 못했어요. 다시 시도해 주세요")
    if not valid(result):
        raise AIError("AI 답의 모양이 맞지 않아요. 다시 시도해 주세요")
    return result, model, usage


# ---------- AI 답 검증: 모양만이 아니라 '내용'이 [데이터]에 있는지 대조합니다 ----------
NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(만|억)?")
SUM_PAIRS = (("pc", "mobile"), ("월간검색량_PC", "월간검색량_모바일"))


def data_numbers(data):
    """[데이터] 안의 숫자를 모두 모읍니다. PC+모바일 합계와 모바일 비율(%)도 함께 넣습니다."""
    found = set()

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            found.add(float(v))
        elif isinstance(v, str):
            for n, unit in NUM_RE.findall(v):
                found.add(float(n.replace(",", "")))
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for a, b in SUM_PAIRS:
                if isinstance(v.get(a), int) and isinstance(v.get(b), int) and v[a] + v[b]:
                    found.add(float(v[a] + v[b]))
                    found.add(round(v[b] / (v[a] + v[b]) * 100, 1))
            for x in v.values():
                walk(x)
    walk(data)
    return found


def unknown_numbers(text, allowed):
    """글 속 숫자 중 [데이터]에서 찾을 수 없는 것을 돌려줍니다.
    12 이하의 정수(월·개수)와 연도는 건너뜁니다. '1,590만'처럼 줄여 쓴 값은 1% 안의 차이를 허용합니다."""
    bad = []
    for m in NUM_RE.finditer(text or ""):
        raw, unit = m.group(1), m.group(2)
        n = float(raw.replace(",", "")) * {"만": 1e4, "억": 1e8}.get(unit, 1)
        if not unit and (n.is_integer() and (n <= 12 or 2016 <= n <= 2035)):
            continue
        tol = lambda a: abs(a) * 0.01 if unit else max(0.051, abs(a) * 0.001)
        if not any(abs(a - n) <= tol(a) for a in allowed) and not any(abs(a * 100 - n) <= 0.51 for a in allowed if a < 1):
            bad.append(m.group(0).strip())
    return bad


def kw_norm(w):
    return str(w).replace(" ", "").lower()


# 경쟁·붐빔을 판단하는 말. 블로그 글 수를 모르는 키워드에 이런 말을 하면 근거 없는 판단입니다.
COMPETE_RE = re.compile(r"경쟁|붐비|붐빕|포화|레드오션|블루오션|유리|공략하기 좋")
UNKNOWN_RE = re.compile(r"확인 필요|확인되지 않|알 수 없|모르|판단하지 않|판단할 수 없")


def compete_claims(*texts):
    """'글 수 확인 필요'처럼 모른다고 한 말은 빼고, 경쟁을 판단한 말만 돌려줍니다.
    "알 수 없지만, …경쟁이 적을 수 있다"처럼 한 문장 안에서 말을 바꾸는 경우를 잡으려고 쉼표·'지만' 단위로 나눠 봅니다."""
    out = []
    for t in texts:
        for sent in re.split(r"(?<=[.!?。])\s+|\n|,|，|(?<=지만)\s", t or ""):
            if COMPETE_RE.search(sent) and not UNKNOWN_RE.search(sent):
                out.append(sent.strip())
    return out


def check_insight(x, data, allowed_words, known_docs=()):
    """AI 인사이트 답을 [데이터]와 대조: 없는 키워드는 묶음에서 빼고, 없는 숫자·키워드는 표시합니다."""
    nums, words = data_numbers(data), {kw_norm(w) for w in allowed_words}
    issues = []

    def nums_in(where, *texts):
        bad = [b for t in texts for b in unknown_numbers(t, nums)]
        issues.extend({"where": where, "type": "숫자", "value": b} for b in bad)
        return bad
    nums_in("판정 요약", x["verdict"]["summary"])
    for sg in x["signals"]:
        sg["unverified"] = nums_in(f"판단 근거 · {sg['name']}", sg["evidence"], sg["meaning"])
    for c in x["clusters"]:
        drop = [w for w in c["keywords"] if kw_norm(w) not in words]
        issues.extend({"where": f"주제 묶음 · {c['name']}", "type": "키워드", "value": w, "removed": True} for w in drop)
        c["keywords"] = [w for w in c["keywords"] if kw_norm(w) in words]
    known = {kw_norm(w) for w in known_docs}
    for t in x["topics"]:
        t["unverified"] = nums_in(f"글 주제 · {t['title']}", t["why"])
        t["keyword_ok"] = kw_norm(t["target_keyword"]) in words
        if not t["keyword_ok"]:
            issues.append({"where": f"글 주제 · {t['title']}", "type": "키워드", "value": t["target_keyword"]})
        t["no_docs_claim"] = [] if kw_norm(t["target_keyword"]) in known else compete_claims(t["why"])
        issues.extend({"where": f"글 주제 · {t['target_keyword']}", "type": "글 수 모르는데 경쟁 판단", "value": c} for c in t["no_docs_claim"])
    return {"ok": not issues, "issues": issues}


def check_expand(x, data):
    """키워드 확장 AI 답을 보낸 목록과 대조합니다."""
    nums = data_numbers(data)
    words = {kw_norm(r["키워드"]) for r in data["키워드목록"]} | {kw_norm(w) for w in data["시드키워드"]}
    docs = {kw_norm(r["키워드"]): r["블로그글"] for r in data["키워드목록"]}
    issues = []
    for p in x["picks"]:
        p["unverified"] = unknown_numbers(p["evidence"], nums) + unknown_numbers(p["why"], nums)
        p["no_docs_claim"] = [] if docs.get(kw_norm(p["keyword"])) is not None else compete_claims(p["evidence"], p["why"])
        issues.extend({"where": f"추천 {p['rank']}위 · {p['keyword']}", "type": "글 수 모르는데 경쟁 판단", "value": c} for c in p["no_docs_claim"])
        issues.extend({"where": f"추천 {p['rank']}위 · {p['keyword']}", "type": "숫자", "value": b} for b in p["unverified"])
        p["keyword_ok"] = kw_norm(p["keyword"]) in words
        if not p["keyword_ok"]:
            issues.append({"where": f"추천 {p['rank']}위", "type": "키워드", "value": p["keyword"]})
    for g in x["groups"]:
        drop = [w for w in g["keywords"] if kw_norm(w) not in words]
        issues.extend({"where": f"묶음 · {g['name']}", "type": "키워드", "value": w, "removed": True} for w in drop)
        g["keywords"] = [w for w in g["keywords"] if kw_norm(w) in words]
    for a in x["avoid"]:
        if kw_norm(a["keyword"]) not in words:
            issues.append({"where": "피할 키워드", "type": "키워드", "value": a["keyword"]})
    return {"ok": not issues, "issues": issues}


def valid_insight(x):
    """AI 답이 화면에 필요한 모양인지 확인 (빠진 칸이 있으면 쓰지 않음)."""
    try:
        return (x["verdict"]["level"] and isinstance(x["verdict"]["summary"], str)
                and all({"name", "evidence", "meaning"} <= set(i) for i in x["signals"])
                and all({"name", "keywords"} <= set(i) for i in x["clusters"])
                and all({"title", "target_keyword", "why"} <= set(i) for i in x["topics"])
                and {"title", "reader", "sections", "checklist"} <= set(x["outline"]))
    except (KeyError, TypeError):
        return False


def ai_insight(keyword, provider=""):
    try:
        provider = pick_provider(provider)
    except AIError as e:
        return 200, {"error_note": f"AI 인사이트: {e}"}
    hit = _ai_cache.get((provider, keyword))
    if hit and time.time() - hit[0] < 3600:
        return 200, {**hit[1], "cached": True}

    # 1) KEYWAVE가 가진 실제 데이터 모으기
    a = analyze(keyword)
    sites = suggest_all(keyword)
    vol = lambda x: (x["pc"] if isinstance(x["pc"], int) else 0) + (x["mobile"] if isinstance(x["mobile"], int) else 0)
    top_rel = [x["keyword"] for x in sorted(a["related"], key=vol, reverse=True)]  # 보여 주는 연관 키워드 20개 모두
    docs = blog_totals(top_rel) if OPEN_API else {}
    rel_docs = [{"키워드": k, "블로그누적발행량": docs.get(k),
                 "발행량_나누기_월검색량": round(docs[k] / vol(r), 1) if docs.get(k) is not None and vol(r) else None}
                for k in top_rel for r in a["related"] if r["keyword"] == k]
    data = {
        "키워드": keyword,
        "월간검색량_PC": a["pc"], "월간검색량_모바일": a["mobile"],
        "블로그_누적발행량": a["docs"], "광고경쟁": a["comp"],
        "메인_발행량_나누기_월검색량": (round(a["docs"] / ((a["pc"] if isinstance(a["pc"], int) else 0) + (a["mobile"] if isinstance(a["mobile"], int) else 0)), 1)
                                 if a["docs"] is not None and isinstance(a["pc"], int) and isinstance(a["mobile"], int) and (a["pc"] + a["mobile"]) else None),
        "최근12개월_데이터랩상대지수": a["trend"],
        # 검색광고 연관 키워드 중 메인 키워드가 들어간 것만, 검색량 순 위 20개
        "연관키워드_검색광고_키워드포함_검색량순20": a["related"],
        "연관키워드_개수": a["related_info"] and {"검색광고_전체": a["related_info"]["total"],
                                             "키워드포함": a["related_info"]["matched"]},
        # 연관 키워드(최대 20개)의 블로그 누적 글 수 — 키워드끼리 경쟁을 비교할 수 있게
        "연관키워드_블로그누적발행량": rel_docs,
        "사이트별_자동완성키워드": {k: v for k, v in sites.items() if v},
        "못가져온_데이터": a["errors"],
    }
    try:
        result, model, usage = call_ai(provider, AI_SYSTEM, data, AI_SCHEMA, valid_insight)
    except AIError as e:
        return 200, {"error_note": f"AI 인사이트: {e}"}
    allowed = [keyword] + [r["keyword"] for r in a["related"]] + [w for v in sites.values() if v for w in v]
    known_docs = ([keyword] if a["docs"] is not None else []) + [k for k, n in docs.items() if n is not None]
    check = check_insight(result, data, allowed, known_docs)
    out = {"keyword": keyword, "provider": provider, "insight": result, "check": check, "model": model, "usage": usage}
    _ai_cache[(provider, keyword)] = (time.time(), out)
    return 200, out


# ---------- 🔎 키워드 확장 + AI 추천 ----------
EXPAND_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "groups", "picks", "avoid"],
    "properties": {
        "summary": {"type": "string"},
        "groups": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                   "required": ["name", "intent", "keywords"],
                   "properties": {"name": {"type": "string"},
                                  "intent": {"type": "string", "enum": ["정보 찾기", "구매·비교", "장소·예약", "기타"]},
                                  "keywords": {"type": "array", "items": {"type": "string"}}}}},
        "picks": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                  "required": ["keyword", "rank", "evidence", "why", "title_idea"],
                  "properties": {"keyword": {"type": "string"}, "rank": {"type": "integer"},
                                 "evidence": {"type": "string"}, "why": {"type": "string"}, "title_idea": {"type": "string"}}}},
        "avoid": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                  "required": ["keyword", "why"],
                  "properties": {"keyword": {"type": "string"}, "why": {"type": "string"}}}},
    },
}

EXPAND_SYSTEM = """당신은 한국 블로그 키워드 전략가입니다. [데이터]의 키워드 목록만 근거로 블로그 글로 쓸 키워드를 고르세요.
규칙:
- [데이터]에 없는 키워드나 숫자를 만들지 마세요. evidence에는 [데이터]의 값(월검색량, 블로그글, 글나누기검색량)을 그대로 적으세요.
- "글나누기검색량"(블로그 누적 글 수 ÷ 월 검색량)이 낮을수록 검색에 비해 글이 적어 덜 붐빕니다. 경쟁 비교는 이 값으로 하세요.
- 블로그글이 null인 키워드는 경쟁을 판단하지 말고, 고를 경우 evidence에 "글 수 확인 필요"라고 적으세요.
- "광고경쟁"은 광고 입찰 경쟁이지 블로그 상위 노출 난이도가 아닙니다.
- 시드 키워드와 주제가 동떨어진 키워드(예: 시드가 '차박'인데 '추석')는 picks에 넣지 말고, 필요하면 avoid에 적으세요.
- groups: 키워드를 검색 의도별로 3~6개 묶음. picks: 가장 좋은 5~8개를 rank 1부터. avoid: 피할 키워드 0~5개.
- 상위 노출을 장담하지 마세요. "노출 확률이 높다", "상위 노출된다" 대신 "덜 붐벼서 유리할 수 있다"처럼 쓰세요.
- 모든 문장은 쉬운 한국어로 짧게 쓰세요."""


def valid_expand(x):
    try:
        return (isinstance(x["summary"], str)
                and all({"name", "intent", "keywords"} <= set(g) for g in x["groups"])
                and all({"keyword", "rank", "evidence", "why", "title_idea"} <= set(p) for p in x["picks"])
                and all({"keyword", "why"} <= set(a) for a in x["avoid"]))
    except (KeyError, TypeError):
        return False


def ai_expand(body):
    """키워드 확장 화면에서 보낸 목록(필터·정렬된 상위 최대 80개)을 AI가 골라 줍니다."""
    try:
        provider = pick_provider(str(body.get("provider", "")))
    except AIError as e:
        return 200, {"error_note": f"AI 추천: {e}"}
    seeds = [str(x)[:50] for x in body.get("seeds", [])][:5]
    num = lambda v: v if isinstance(v, (int, float)) else None
    src = [r for r in body.get("rows", [])[:80] if isinstance(r, dict) and str(r.get("keyword", "")).strip()]
    # 화면은 블로그 글 수를 위쪽 일부만 불러와서, AI에게 보내기 전에 빈 칸을 모두 채웁니다.
    # (글 수를 모르면 AI가 경쟁을 짐작해 버려서 — 실제로 '경쟁이 적다'던 키워드가 가장 붐볐어요)
    missing = [str(r["keyword"])[:60] for r in src if num(r.get("docs")) is None]
    filled = blog_totals(missing, limit=80) if missing and OPEN_API else {}
    rows = []
    for r in src:
        pc, mo = r.get("pc"), r.get("mobile")
        docs = num(r.get("docs")) if num(r.get("docs")) is not None else filled.get(str(r["keyword"])[:60])
        tot = (pc if isinstance(pc, int) else 0) + (mo if isinstance(mo, int) else 0)
        rows.append({"키워드": str(r["keyword"])[:60],
                     "월검색량": tot if isinstance(pc, int) or isinstance(mo, int) else None,
                     "광고경쟁": r.get("comp"), "블로그글": docs,
                     "글나누기검색량": round(docs / tot, 1) if docs is not None and tot else None})
    if not rows:
        return 400, {"error": "AI에게 보낼 키워드가 없어요. 먼저 키워드 확장을 해 주세요"}
    key = (provider, tuple(seeds), json.dumps(rows, ensure_ascii=False, sort_keys=True))
    hit = _ai_cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return 200, {**hit[1], "cached": True}
    data = {"시드키워드": seeds, "키워드목록": rows}
    try:
        result, model, usage = call_ai(provider, EXPAND_SYSTEM, data, EXPAND_SCHEMA, valid_expand)
    except AIError as e:
        return 200, {"error_note": f"AI 추천: {e}"}
    out = {"provider": provider, "result": result, "check": check_expand(result, data), "model": model, "usage": usage, "sent": len(rows),
           "docs_filled": {k: n for k, n in filled.items() if n is not None}}
    _ai_cache[key] = (time.time(), out)
    return 200, out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def guard(self):
        """/healthz(호스팅 상태 확인)는 통과, 나머지는 모두 허용합니다."""
        if urllib.parse.urlparse(self.path).path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return False
        return True

    def do_HEAD(self):
        if self.guard():
            super().do_HEAD()

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/setup", "/api/ai_expand"):
            return self.send_error(404)
        # 다른 웹사이트가 몰래 요청하지 못하도록, KEYWAVE 화면에서 온 요청만 받습니다
        port = self.server.server_address[1]
        if DEPLOYED:  # 배포: 접속한 주소(Host)와 같은 곳에서 온 요청만
            origin = urllib.parse.urlparse(self.headers.get("Origin") or "").netloc
            same = bool(origin) and origin == self.headers.get("Host")
        else:         # 내 컴퓨터: localhost 화면에서 온 요청만
            same = self.headers.get("Origin") in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")
        if not same:
            return self.send_json(403, {"error": "KEYWAVE 화면에서만 할 수 있어요."})
        if path == "/api/setup" and DEPLOYED:
            return self.send_json(403, {"error": "인터넷에 올린 KEYWAVE에서는 화면에서 열쇠를 저장할 수 없어요. Vercel → Settings → Environment Variables에 넣어 주세요."})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:  # 요청 내용이 잘못된 경우 (서버 잘못이 아니므로 400)
            return self.send_json(400, {"error": "요청 내용이 잘못됐어요. 화면에서 다시 해 주세요."})
        if path == "/api/ai_expand":
            try:
                return self.send_json(*ai_expand(body))
            except Exception as e:
                return self.send_json(500, {"error": f"AI 추천 오류: {e}"})
        try:
            self.send_json(*save_and_test(body))
        except Exception as e:
            self.send_json(500, {"error": f"저장 실패: {e}"})

    def do_GET(self):
        if not self.guard():
            return
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/keys":
            return self.send_json(200, key_status())
        if url.path == "/api/ai":
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0].strip()
            if not q:
                return self.send_json(400, {"error": "키워드를 입력하세요"})
            try:
                prov = urllib.parse.parse_qs(url.query).get("provider", [""])[0]
                return self.send_json(*ai_insight(q, prov))
            except Exception as e:
                return self.send_json(502, {"error": f"AI 인사이트 오류: {e}"})
        if url.path == "/api/docs":
            if not OPEN_API:
                return self.send_json(200, {"error_note": "블로그 글 수는 NAVER API HUB 열쇠가 있어야 나와요", "docs": {}})
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
            return self.send_json(200, {"docs": blog_totals(q.split("\n"))})
        if url.path == "/api/trend":
            try:
                return self.send_json(*trend_detail(urllib.parse.parse_qs(url.query)))
            except Exception as e:
                return self.send_json(502, {"error": f"네이버 API 오류: {e}"})
        api = {"/api/analyze": analyze, "/api/suggest": suggest_all, "/api/trending": trending, "/api/expand": expand}.get(url.path)
        if not api:
            if any(part.startswith(".") for part in url.path.split("/")) or url.path.endswith(".py"):
                return self.send_error(404)
            return super().do_GET()
        q = urllib.parse.parse_qs(url.query).get("q", [""])[0].strip()
        try:
            if not q and api is not trending:
                status, body = 400, {"error": "키워드를 입력하세요"}
            else:
                status, body = 200, api(q)
        except Exception as e:  # API 오류를 화면에 그대로 보여줍니다
            status, body = 502, {"error": f"네이버 API 오류: {e}"}
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8765"))
    mode = ("검색광고 키 있음 — 검색량 실제 데이터" if AD_KEY and AD_SECRET and AD_CUSTOMER else "검색광고 키 없음 — 검색량은 비어 있음") + \
           (f" / {OPEN_API['name']} 키 있음" if OPEN_API else " / HUB 키 없음 — 발행량·그래프 비어 있음")
    host = os.getenv("HOST", "0.0.0.0" if DEPLOYED else "127.0.0.1")
    if DEPLOYED:
        mode += " / 배포 모드"
    print(f"KEYWAVE http://{'localhost' if host == '127.0.0.1' else host}:{port}  [{mode}]", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
