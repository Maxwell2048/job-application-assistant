"""Collect public SEEK search pages and full job descriptions without login.

Expanded focused mode searches level and role keywords across Perth categories
and nationwide remote roles; broad also includes the whole Perth ICT classification. Both cover the
work types in --worktypes (default full time and contract/temp); the internship
lanes also cover casual/vacation, where vacation programmes are listed. Cards whose
subclassification or title rules them out are dropped before any detail page
is fetched, and jobs listed in --known-ids are reported without re-reading
their details.

Expanded searches rotate lanes and persist their next page. A search page is
read before the JD backlog, so discovery is not starved. Legacy custom keyword
searches retain details-first behavior.

A refused request is classified, not just counted: an anti-bot challenge (the edge answers with a "Just a moment" body and cf-mitigated: challenge, no Retry-After) differs from a plain rate limit that does carry Retry-After. Either one ends the round, but both write a cool-down file and keep every unread job id in <state-dir>/pending_details.txt, so the next round drains them through --pending-ids without needing the ledger to hold those rows at all (--backlog-only runs such a drain with zero search requests). While the cool-down is still open the next round makes no request of its own unless it can use another transport.

Two transports are available. The default one presents a browser transport-layer
(TLS) fingerprint via curl_cffi (--impersonate). When that transport gets
refused, --transport auto falls back to a real local Chromium browser run headless
(--browser-path), which passes checks the scripted client cannot; measured on this
machine: 4.5-5.6 s per warm navigation versus ~12-25 min of lockout for the script.
Velocity is also capped before the edge objects: jittered gaps between requests and
a rolling hourly budget for search pages and detail pages persisted in the state dir."""
import argparse
import csv
import hashlib
import json
import re
import subprocess
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

# Transport-layer (TLS) fingerprint spoofing: curl_cffi wraps curl-impersonate so the
# ClientHello matches a real browser, which anti-bot systems fingerprint. Optional: when
# absent the collector falls back to the standard library (no fingerprint spoofing).
try:
    from curl_cffi import requests as _curl_requests
except ImportError:
    _curl_requests = None

from search_rules import excluded_title, ambiguous_level, clearly_non_it_title, it_evidence

ROOT = Path(__file__).resolve().parent
DEFAULT_IMPERSONATE = 'chrome131'   # browser TLS fingerprint to present; override with --impersonate

# --- anti-bot handling -------------------------------------------------------
# Measured on this machine: an eight-request burst of search pages spaced exactly at the
# delay was refused for ~12-25 minutes afterwards, and the refusal carried no Retry-After,
# so waiting blindly is guesswork. These files make the wait explicit instead.
BLOCK_FILE = 'seek_block.json'        # cool-down state written when a request is refused
QUOTA_FILE = 'seek_quota.json'        # rolling hourly record of what this client asked for
QUEUE_FILE = 'pending_details.txt'    # job ids whose full JD is still owed, one per line
CHALLENGE_COOLDOWN_MINUTES = 25       # anti-bot challenge: long enough to outlast the lockout window
THROTTLE_COOLDOWN_MINUTES = 10        # plain rate limit, or shorter when Retry-After asks for longer
MAX_LIST_PER_HOUR = 12                # search pages cost ~1.2 MB each and drive the refusals
MAX_DETAILS_PER_HOUR = 40             # detail pages are ~230 KB and are what actually earns a JD
JITTER_FACTOR = 1.8                   # a request waits uniform(delay, delay*factor); equal gaps get flagged

BROWSER_CANDIDATES = (                # real Chromium-family browsers that pass scripted clients' checks
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome', '/usr/bin/chromium-browser', '/usr/bin/chromium')
BROWSER_TIMEOUT_SECONDS = 90          # one navigation, including any challenge round trip
BROWSER_VIRTUAL_TIME_MS = 20000       # how long the browser may spend settling a page before dump-dom returns

ROLE_KEYWORDS = ['software developer', 'data analyst', 'business intelligence', 'application support', 'service desk', 'test analyst', 'AI engineer', 'data engineer', 'project coordinator', 'machine learning', 'python developer', 'web developer', 'IT support', 'business analyst']
INTERN_KEYWORDS = ['internship', 'intern']
MODE_KEYWORDS = {'focused': ['graduate', 'junior', 'entry level', 'trainee', *INTERN_KEYWORDS, *ROLE_KEYWORDS], 'broad': ['']}
# Summer and vacation programmes are usually listed as Casual/Vacation, a work type the
# default search leaves out; only the internship lanes add it.
VACATION_WORKTYPE = '245'


def lane_worktypes(keyword, worktypes):
    if keyword in INTERN_KEYWORDS and VACATION_WORKTYPE not in worktypes.split(','):
        return worktypes + ',' + VACATION_WORKTYPE
    return worktypes

def search_plan(mode, keywords, remote=True):
    # Interleave regional and remote lanes, rather than leaving remote until the end.
    plan = [(k, 'ict') for k in keywords if k not in ROLE_KEYWORDS] if mode == 'focused' else [('', 'ict')]
    roles = [k for k in keywords if k in ROLE_KEYWORDS] if mode == 'focused' else ROLE_KEYWORDS
    for keyword in roles:
        cross_category = keyword in {'software developer', 'data analyst', 'business intelligence', 'AI engineer', 'data engineer', 'machine learning', 'python developer', 'web developer'}
        plan.append((keyword, 'perth_all' if cross_category else 'ict'))
        if remote:
            plan.append((keyword + ' remote', 'remote' if cross_category else 'remote_ict'))
    return plan
# Quoted so SEEK matches the phrase, not each word; plain "graduate" also matches "postgraduate".
QUOTED = {'graduate', 'entry level', 'intern'}   # a quoted "intern" does not match "internal"
WORKTYPES = '242,244'   # Full time, Contract/Temp
EXCLUDED_SUBCLASSES = 'Management,Architects,Team Leaders,CTO/CIO'
EXCLUDED_TITLE = r'\b(senior|sr\.?|lead|principal|manager|head|director|architect|mid[- ]?level|intermediate)\b'


def safe_csv(value):
    if isinstance(value, list):
        value = ' | '.join(value)
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def restricted_citizenship(text):
    identity = r'(?:Australian\s+citizen(?:ship|s)?|permanent\s+residen\w*)'
    for clause in re.split(r'[;\n]|(?<=[.!?])\s+|,\s*(?=no\s+(?:visa|sponsor))', text):
        if re.search(r'\bor\s+(?:(?:have|hold)\s+)?(?:a\s+)?(?:valid\s+)?(?:visa|full\s+(?:working|work)\s+rights)', clause, re.I):
            continue
        if re.search(identity + r'.{0,45}\b(?:not required|not necessary|optional|preferred)\b', clause, re.I):
            continue
        if re.search(r'\b(?:not required to|do not need to|need not)\s+(?:be|hold)\b', clause, re.I):
            continue
        if re.search(r'\b(?:must|required to|need to)\s+(?:be|hold|have)\s+(?:an?\s+)?' + identity, clause, re.I) or re.search(identity + r'.{0,45}\b(?:only|required|mandatory)\b', clause, re.I):
            return True
    return False


def junior_evidence(title, jd):
    term = r'\b(?:graduates?|junior|entry[ -]level|interns?|internships?|vacation (?:program|programme|role)s?)\b'
    if re.search(term, title, re.I):
        return True
    for sentence in re.split(r'[;\n]|(?<=[.!?])\s+', jd):
        if re.search(r'\b(?:mentor|supervis|manag|coach|train|lead)\w*\b.{0,55}' + term, sentence, re.I):
            continue
        if re.search(r'(?:\b(?:seeking|hiring|looking for|suitable for|open to|welcome)\b.{0,45}' + term + r'|' + term + r'.{0,35}\b(?:role|position|opportunity|welcome|encouraged)\b)', sentence, re.I):
            return True
    return False


def state_from_html(html):
    match = re.search(r'window\.SEEK_REDUX_DATA\s*=\s*', html)
    if not match:
        raise ValueError('SEEK page state missing: access challenge or changed page layout')
    # Parse JSON only; never execute page JavaScript.
    return json.JSONDecoder().raw_decode(html[match.end():])[0]


# --- refusal classification, cool-down, backlog queue and hourly quota --------

class Headers:
    """Case-insensitive header view so callers can test cf-mitigated without caring how the provider spelled it."""

    def __init__(self, source=None):
        self._items = {str(k).lower(): v for k, v in dict(source or {}).items()}

    def get(self, name, default=None):
        return self._items.get(str(name).lower(), default)

    def items(self):
        return self._items.items()


CHALLENGE_MARKERS = ('just a moment', 'attention required', 'cf-chl', 'turnstile')


def classify_refusal(code, headers=None, body=''):
    """What refused us. An anti-bot challenge and a rate limit need different answers: one is a
    fingerprint/session problem that waiting out helps little, the other is pure velocity."""
    headers = headers if isinstance(headers, Headers) else Headers(headers or {})
    retry_after = None
    raw = str(headers.get('retry-after') or '').strip()
    if raw.isdigit():
        retry_after = min(max(int(raw), 1), 3600)
    text = (body or '')[:2000].lower()
    challenged = str(headers.get('cf-mitigated') or '').lower().startswith('challenge') \
        or any(marker in text for marker in CHALLENGE_MARKERS) \
        or bool(headers.get('server-timing')) and 'chlray' in str(headers.get('server-timing')).lower()
    kind = 'challenge' if (challenged or code == 403) else 'throttle'
    return dict(kind=kind, code=int(code), retry_after=retry_after)


def block_info(exc):
    """The refusal facts carried by an exception, or None when it is not a block at all."""
    code = getattr(exc, 'code', None)
    if not isinstance(code, int) or code not in (403, 429):
        return None
    return classify_refusal(code, getattr(exc, 'headers', None), getattr(exc, 'seek_body', ''))


BLOCK_LABELS = {'challenge': '反爬校验拦截', 'throttle': '请求频率限制'}


def load_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json(path, data):
    """Best effort: losing cool-down state must never lose the run's collected output."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
        return True
    except OSError:
        return False


def cooldown_open(state, now=None):
    until = (state or {}).get('until')
    if not until:
        return False
    try:
        end = datetime.fromisoformat(until)
    except ValueError:
        return False
    return (now or datetime.now(timezone.utc)) < end


QUEUE_LIMIT = 500


def load_queue(path):
    """Job ids still owed a full JD, in the order they were promised."""
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    seen, ids = set(), []
    for line in lines:
        ident = line.strip()
        if ident.isdigit() and ident not in seen:
            seen.add(ident)
            ids.append(ident)
    return ids


def save_queue(path, ids):
    """Bound the backlog so an abandoned keyword lane cannot grow it without limit."""
    unique, seen = [], set()
    for ident in ids:
        ident = str(ident).strip()
        if ident.isdigit() and ident not in seen:
            seen.add(ident)
            unique.append(ident)
        if len(unique) >= QUEUE_LIMIT:
            break
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text('\n'.join(unique) + ('\n' if unique else ''), encoding='utf-8')
        temp.replace(path)
        return True
    except OSError:
        return False


class QuotaLedger:
    """A rolling hourly budget kept on disk, because several rounds inside one hour add up to a refusal.

    Only requests that actually left the machine are counted; cache hits cost nothing."""

    def __init__(self, path, list_per_hour=MAX_LIST_PER_HOUR, details_per_hour=MAX_DETAILS_PER_HOUR):
        self.path = Path(path)
        self.limits = {'list': max(0, int(list_per_hour)), 'detail': max(0, int(details_per_hour))}
        self.persisted = True
        events = load_json(self.path).get('events') or []
        cutoff = time.time() - 3600
        self.events = [e for e in events if isinstance(e, dict) and float(e.get('at') or 0) > cutoff]

    def used(self, kind):
        return sum(1 for e in self.events if e.get('kind') == kind)

    def remaining(self, kind):
        limit = self.limits.get(kind)
        return 0 if limit is None else max(0, limit - self.used(kind))

    def allow(self, kind):
        return bool(self.remaining(kind))

    def spend(self, kind):
        self.events.append(dict(kind=kind, at=time.time()))
        self.persisted = save_json(self.path, dict(events=self.events[-200:], updated_at=datetime.now(timezone.utc).isoformat()))


def find_browser(explicit=None):
    """A real Chromium browser to fall back on when the scripted client is refused."""
    import shutil
    candidates = []
    if explicit:
        candidates.append(str(explicit))
    for name in ('chrome', 'google-chrome', 'chromium', 'chromium-browser', 'microsoft-edge', 'msedge'):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(BROWSER_CANDIDATES)
    for candidate in candidates:
        try:
            if Path(candidate).is_file():
                return candidate
        except OSError:
            continue
    return None


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(html):
    parser = PlainText()
    parser.feed(html)
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def search_term(keyword):
    return f'"{keyword}"' if keyword in QUOTED else keyword


def search_url(keyword, page, worktypes=WORKTYPES, scope='ict'):
    # /graduate-jobs redirects to graduatesearch=true, which is NOT keyword search; the
    # keyword goes in the query string instead. An empty keyword lists the whole classification.
    params = dict(keywords=search_term(keyword)) if keyword else {}
    params.update(worktype=worktypes, sortmode='ListedDate', page=page)
    path = ('jobs-in-information-communication-technology/in-All-Australia' if scope == 'remote_ict' else 'jobs-in-information-communication-technology/in-Perth-WA-6000') if scope in ('ict', 'remote_ict') else ('jobs/in-All-Australia' if scope == 'remote' else 'jobs/in-Perth-WA-6000')
    return 'https://au.seek.com/' + path + '?' + urllib.parse.urlencode(params)


def parse_search(html, keyword, worktypes=WORKTYPES, scope='ict'):
    state = state_from_html(html)
    query = state['search']['query']
    expected_keyword = search_term(keyword) if keyword else ''
    for key, expected in dict(keywords=expected_keyword, classification='6281' if scope in ('ict','remote_ict') else '', worktype=worktypes, where='All Australia' if scope in ('remote','remote_ict') else 'Perth WA 6000').items():
        if str(query.get(key, '')).casefold() != expected.casefold():
            raise ValueError(f'Search filter changed: {key}={query.get(key)!r}, expected {expected!r}')
    results = state['results']
    if results.get('isError'):
        raise ValueError('SEEK reported a search error')
    cards = results['results']['jobs']
    if cards is None and results['results'].get('isZeroResults'):
        cards = []
    if not isinstance(cards, list):
        raise ValueError('Unexpected search jobs structure')
    # SSR totalPages is often the placeholder 0 even with a visible Next link.
    visible_pages = [int(n) for n in re.findall(r'data-automation="page-(\d+)"', html)]
    last_visible_page = max(visible_pages + [int(query.get('page', 1))]) if cards else 0
    return cards, last_visible_page, results.get('totalCount')


def subclasses(card):
    return [x.get('subclassification', {}).get('description', '') for x in card.get('classifications', [])]


def card_exclusion(card, excluded_subclasses, title_pattern):
    """Why a search card is not worth a detail request, or ''."""
    if clearly_non_it_title(card.get('title', '')):
        return '明确非 IT 职业，搜索入口排除'
    excluded = {s.strip().casefold() for s in excluded_subclasses if s.strip()}
    for sub in subclasses(card):
        if sub.casefold() in excluded and not ambiguous_level(card.get('title', '')):
            return f'子分类 {sub}'
    if excluded_title(card.get('title', ''), title_pattern):
        return '标题含资深或管理级别'
    return ''


def detail_info(html, expected_id):
    """The JD text and the listing facts of a detail page."""
    result = state_from_html(html)['jobdetails']['result']
    job = result['job']
    if str(job['id']) != str(expected_id):
        raise ValueError('Job ID mismatch on detail page')
    content = plain(job.get('content2', ''))
    if not content:
        raise ValueError('Full JD absent from detail page')
    label = lambda key: ((job.get(key) or {}).get('label') or '') if isinstance(job.get(key), dict) else ''  # noqa: E731
    return dict(description=content, expired=bool(job.get('isExpired')), title=job.get('title') or '',
                company=(job.get('advertiser') or {}).get('name') or '', location=label('location'),
                work_type=label('workTypes'), salary=label('salary'),
                listed_at=((job.get('listedAt') or {}).get('dateTimeUtc') or ''))


def parse_detail(html, expected_id):
    info = detail_info(html, expected_id)
    return info['description'], info['expired']


def read_detail(fetch, job, errors):
    """Fill one job from its detail page. -> 'ok', 'error' (go on with the next),
    'quota' (this hour's share is spent; the job stays owed) or 'stop' (the edge refused us,
    or the page/transport failed: no further requests this run)."""
    try:
        info = detail_info(fetch.get(job['url']), job['job_id'])
    except QuotaExhausted:
        # Not a fault of this listing: leave it pending so the backlog queue still owes it.
        return 'quota'
    except urllib.error.HTTPError as exc:
        refused = block_info(exc)
        job['detail_status'] = '读取失败（访问拒绝或限流）' if refused else '读取失败（HTTP错误）'
        job['detail_error'] = f'HTTP {exc.code}'          # keep the bare code machine-readable downstream
        if refused:
            job['block_code'], job['block_kind'] = refused['code'], refused['kind']
        errors.append(f"JD {job['job_id']}: HTTP {exc.code}")
        return 'stop' if refused else 'error'
    except Exception as exc:
        job['detail_status'] = '读取失败（页面或连接错误）'
        job['detail_error'] = str(exc)
        errors.append(f"JD {job['job_id']}: {exc}")
        return 'stop'
    job['description'], job['expired'] = info['description'], info['expired']
    for key in ('title', 'company', 'location', 'work_type', 'salary', 'listed_at'):
        if info[key] and not job.get(key):
            job[key] = info[key]
    job['detail_status'] = '已读取完整JD'
    print(f"JD {job['job_id']}: {len(job['description'])} characters", flush=True)
    return 'ok'


def blocked_by(job):
    """True when a stopped row was stopped by the edge, not by an unreadable page."""
    if job.get('block_kind'):
        return str(job.get('block_code') or '') in ('403', '429')
    return str(job.get('detail_error') or '') in ('HTTP 403', 'HTTP 429')   # rows written before these fields existed


def assess(job):
    jd = job.get('description', '')
    evidence = [s.strip() for s in re.split(r'\n|(?<=[.!?])\s+', jd)
                if re.search(r'citizen|permanent residen|visa|sponsor|right to work|work rights|clearance', s, re.I)]
    job['eligibility_485'] = '待核实'
    # Only flag strong explicit restrictions; preserve evidence for human review.
    restricted = restricted_citizenship(jd)
    if restricted:
        job['eligibility_485'] = '疑似公民或PR限制，需复核原文'
    job['eligibility_evidence'] = '\n'.join(evidence)
    job['level_evidence'] = '\n'.join(s.strip() for s in jd.splitlines()
        if re.search(r'\bgraduate\b|\bjunior\b|entry.level|\binterns?(?:hips?)?\b|\d+.{0,8}years.{0,30}experience', s, re.I))
    reasons = []
    if not jd:
        reasons.append('尚未读取完整JD')
    if re.search(r'\b(senior|principal|manager|lead)\b', job['title'], re.I):
        reasons.append('标题含资深或管理级别')
    if not junior_evidence(job['title'], jd):
        reasons.append('缺少初级岗位明确证据')
    if re.search(r'\b(?:[3-9]|\d{2,})\s*(?:\+|[-–]\s*\d+)?\s*years?\b.{0,45}\bexperience\b', jd, re.I):
        reasons.append('含3年以上经验线索，需核实是否为必需条件')
    if re.search(r'\b(FIFO|BIBO|DIDO)\b', jd, re.I):
        reasons.append('需核实FIFO/异地工作地点')
    if 'perth' not in job['location'].lower():
        reasons.append('需核实是否在Perth范围')
    if not re.search(r'\bfull[ -]time\b', job['work_type'], re.I):
        reasons.append('需核实全职条件')
    if restricted:
        reasons.append('疑似身份限制')
    if job.get('expired'):
        reasons.append('岗位已过期')
    job['review_notes'] = '；'.join(reasons + ['485资格尚未确认'])
    job['priority'] = '优先核实' if jd and not reasons else '需要复核'
    return job


class QuotaExhausted(Exception):
    """This hour's share of requests is spent; stop without asking the edge again."""

    def __init__(self, kind):
        super().__init__(f'hourly {kind} budget exhausted')
        self.kind = kind


class Fetcher:
    """Two transports, one refusal policy.

    The scripted transport is cheap and fast (~0.7 s) but gets refused outright once the edge
    decides this client looks automated -- measured lockouts of 12-25 minutes, no Retry-After to
    wait on. A real local Chromium browser passes where the script cannot (~4.5-5.6 s per warm
    navigation), so a refusal switches the rest of the round over instead of throwing the job away."""

    def __init__(self, cache, delay, refresh, impersonate=DEFAULT_IMPERSONATE, *, transport='auto',
                 browser=None, profile=None, quota=None, jitter_factor=JITTER_FACTOR):
        self.cache, self.delay, self.refresh = cache, delay, refresh
        cache.mkdir(parents=True, exist_ok=True)
        self.last = 0
        self.requests = 0
        self.impersonate = impersonate
        self._session = None
        requested = (transport or 'auto').lower()
        if requested not in ('auto', 'http', 'chrome'):
            raise ValueError('transport must be auto, http or chrome')
        self.policy = requested                                      # what the caller allowed, independent of the current channel
        self.browser_path = browser if requested != 'http' else None  # an explicit http run never shells out
        self.profile = Path(profile) if profile else Path(cache) / '.browser_profile'
        self.quota = quota
        self.jitter_factor = max(1.0, float(jitter_factor))
        if requested == 'chrome':
            if not find_browser(self.browser_path):
                raise ValueError('本机没有可用的 Chromium 浏览器，无法使用浏览器传输通道')
            self.transport = 'chrome'
        else:
            self.transport = 'http'
        self.switched = False           # the scripted client was refused and the browser took over mid-round
        self.block = None               # facts of the last refusal that stopped us: code, kind, retry_after
        # Attempts per transport: a switched request is counted on whichever channel carried it.
        self.http_attempts = self.browser_attempts = 0

    def can_use_browser(self):
        """Whether a refusal may be handed to a real browser instead of ending the round."""
        return self.policy != 'http' and bool(find_browser(self.browser_path))

    def _open_session(self):
        if self._session is None and _curl_requests is not None:
            self._session = _curl_requests.Session(impersonate=self.impersonate)
        return self._session

    def _fetch_http(self, url):
        """GET url -> html text. Raises urllib.error.HTTPError on non-2xx so the existing block logic is unchanged.

        Presents the browser transport-layer (TLS) fingerprint when curl_cffi is available; otherwise the standard library."""
        session = self._open_session()
        if session is not None:
            response = session.get(url, timeout=30)
            if not 200 <= response.status_code < 300:
                exc = urllib.error.HTTPError(url, response.status_code, response.reason or 'HTTP error', Headers(response.headers), None)
                exc.seek_body = response.text[:2000]   # the edge says what it is in prose; use it rather than guessing
                raise exc
            return response.text
        request = urllib.request.Request(url, headers={'User-Agent': 'SeekJobCollector/0.1 (personal job search)', 'Accept': 'text/html'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode('utf-8')

    def _fetch_browser(self, url):
        """One navigation in a real headless Chromium browser (--dump-dom).

        The result is the serialized document, not the bytes on the wire, so callers must validate it before caching; get() already does."""
        executable = find_browser(self.browser_path)
        if not executable:
            raise RuntimeError('本机没有可用的 Chromium 浏览器（可用 --browser-path 指定）')
        self.profile.mkdir(parents=True, exist_ok=True)
        dest = Path(self.cache) / f'.navigating-{uuid.uuid4().hex}.html'
        log = dest.with_suffix('.err.txt')
        command = [executable, '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-crash-reporter', '--no-first-run',
                   f'--virtual-time-budget={BROWSER_VIRTUAL_TIME_MS}', f'--user-data-dir={self.profile}', '--dump-dom', url]
        try:
            with dest.open('wb') as out, log.open('wb') as err:
                subprocess.run(command, stdout=out, stderr=err, timeout=BROWSER_TIMEOUT_SECONDS)
            html = dest.read_text(encoding='utf-8', errors='replace') if dest.exists() else ''
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'浏览器导航超时（{BROWSER_TIMEOUT_SECONDS} 秒）：{exc}') from exc
        finally:
            for leftover in (dest, log):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        if 'SEEK_REDUX_DATA' not in html:
            head = html[:2000].lower()
            refused = any(marker in head for marker in CHALLENGE_MARKERS)
            if refused or not html.strip():
                exc = urllib.error.HTTPError(url, 429, 'Anti-bot challenge', Headers({'cf-mitigated': 'challenge'}), None)
                exc.seek_body = html[:2000]
                raise exc
            raise ValueError('页面结构已变化，未找到站点数据')   # a real page we can no longer parse is not a block to retry
        return html

    def _fetch(self, url):
        """-> html text. A scripted refusal switches this and every later request over to the browser."""
        if self.transport == 'chrome':
            self.browser_attempts += 1
            return self._fetch_browser(url)
        self.http_attempts += 1
        try:
            return self._fetch_http(url)
        except urllib.error.HTTPError as exc:
            info = block_info(exc)
            if info is None or not self.can_use_browser():
                self.block = info or self.block
                raise
            self.block = info
            self.switched = True
            self.transport = 'chrome'
            print(f"脚本请求被拒（HTTP {info['code']}·{BLOCK_LABELS[info['kind']]}），改用本机浏览器继续读取", flush=True)
            time.sleep(max(1.0, self.delay))     # do not follow straight on the heels of the refusal
            self.browser_attempts += 1           # the retry goes out through the other channel in place
            try:
                return self._fetch_browser(url)
            except urllib.error.HTTPError as inner:   # both transports refused: report that one, not the first
                self.block = block_info(inner) or info
                raise

    def _pace(self, url, kind):
        """Deterministic jitter so consecutive requests are not metronomic; equal gaps read as a script."""
        weight = 0.7 if kind == 'detail' else 1.0
        fraction = int(hashlib.sha256(f'{url}|{kind}'.encode()).hexdigest()[:8], 16) / 0xffffffff
        low, high = self.delay * weight, self.delay * weight * self.jitter_factor
        target = low + (high - low) * fraction
        time.sleep(max(0, target - (time.monotonic() - self.last)))
        self.last = time.monotonic()

    @staticmethod
    def request_kind(url):
        return 'detail' if '/job/' in url else 'list'

    def get(self, url):
        dest = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.html')
        if dest.exists() and not self.refresh and time.time() - dest.stat().st_mtime < 21600:
            return dest.read_text(encoding='utf-8')
        kind = self.request_kind(url)
        if self.quota is not None and not self.quota.allow(kind):
            raise QuotaExhausted(kind)
        self._pace(url, kind)
        html = self._fetch(url)
        state_from_html(html)  # Do not cache challenges or unrelated HTML.
        if self.quota is not None:   # only a request that really left the machine spends the hour's share
            self.quota.spend(kind)
        self.requests += 1
        dest.write_text(html, encoding='utf-8')
        return html


def run(args):
    state_dir = Path(getattr(args, 'state_dir', None) or ROOT)
    block_path = Path(getattr(args, 'block_file', None) or state_dir / BLOCK_FILE)
    queue_path = Path(getattr(args, 'queue_file', None) or state_dir / QUEUE_FILE)
    quota = QuotaLedger(Path(getattr(args, 'quota_file', None) or state_dir / QUOTA_FILE),
                        getattr(args, 'max_list_per_hour', MAX_LIST_PER_HOUR),
                        getattr(args, 'max_details_per_hour', MAX_DETAILS_PER_HOUR))
    fetch = Fetcher(state_dir / '.seek_cache', args.delay, args.refresh, getattr(args, 'impersonate', DEFAULT_IMPERSONATE),
                    transport=getattr(args, 'transport', 'auto'), browser=getattr(args, 'browser_path', None),
                    profile=getattr(args, 'profile_dir', None), quota=quota, jitter_factor=getattr(args, 'jitter_factor', JITTER_FACTOR))
    jobs, pages, errors, filtered = {}, [], [], {}
    now = datetime.now(timezone.utc).isoformat()
    block_state = load_json(block_path)
    # A cool-down only means stay home when no other transport can answer; otherwise keep working.
    deferred = cooldown_open(block_state) and fetch.transport != 'chrome' and not fetch.can_use_browser()
    stopped = rate_limited = deferred
    quota_stop = None          # which hourly budget ran out, if any
    if deferred:   # the window is still open: report it without spending a single request on it
        errors.append(f"SEEK 仍在退避中（{BLOCK_LABELS.get(block_state.get('kind'), '访问受限')} HTTP {block_state.get('code')}，"
                      f"约至 {block_state.get('until')}）；本轮未发出请求")
    keywords = args.keywords if args.keywords is not None else MODE_KEYWORDS[args.mode]
    expanded = getattr(args, 'expanded_search', False)
    plan = search_plan(args.mode, keywords, getattr(args, 'include_remote', True)) if expanded and args.keywords is None else [(k, 'ict') for k in keywords]
    cursor_path = state_dir / ('search_cursor_' + args.mode + '.json')
    cursor = {'pages': {}, 'next': 0}
    signature = hashlib.sha256(json.dumps([plan, args.worktypes]).encode()).hexdigest()
    if expanded and cursor_path.exists():
        try:
            saved = json.loads(cursor_path.read_text(encoding='utf-8'))
            if saved.get('signature') == signature:
                cursor = saved
        except (OSError, ValueError):
            pass
    # An empty keyword list is legal (the caller asked only for a backlog drain); modulo by zero is not.
    lanes = len(plan) or 1
    start = int(cursor.get('next', 0)) % lanes
    plan_indices = [(start + i) % lanes for i in range(len(plan))]
    page_budget = args.max_pages * 4 if args.mode == 'focused' else args.max_pages
    excluded_subclasses = args.exclude_subclass.split(',') if args.exclude_subclass else []
    known = set()
    if args.known_ids:
        known = {line.strip() for line in Path(args.known_ids).read_text(encoding='utf-8').splitlines() if line.strip()}
    pending = []

    def add_pending(ident):
        ident = str(ident).strip()
        if ident.isdigit() and ident not in known and ident not in pending:   # a ledger row we hold is never owed
            pending.append(ident)


    if args.pending_ids:   # ids the caller knows only by title, most wanted first (the ledger's own backlog)
        try:
            for line in Path(args.pending_ids).read_text(encoding='utf-8').splitlines():
                add_pending(line)
        except OSError as exc:
            errors.append(f'待补读清单无法读取：{exc}')
    queued_before = load_queue(queue_path)   # ids an interrupted round promised but the ledger cannot know about
    for ident in queued_before:              # no JD text means no ledger row, so this file is what keeps them alive
        add_pending(ident)
    # A drain needs at least one detail slot to spend; a search-only round must still resume its lanes.
    forced_backlog = bool(getattr(args, 'backlog_only', False)) and bool(pending) and args.max_details > 0
    backlog_only = forced_backlog or (expanded and bool(pending) and args.max_details > 0
                                      and len(pending) >= max(args.max_details, 1))
    if backlog_only:
        pending_budget_override = args.max_details
    # Details first: the few requests SEEK allows go to jobs already listed, then to search pages.
    budget = args.max_details
    # Reserve detail capacity for discoveries; large JD backlogs cannot monopolize every run.
    pending_budget = min(budget, max(1, budget // 2)) if expanded and budget else budget
    if backlog_only:
        pending_budget = pending_budget_override
    pending_done = deferred   # while a cool-down holds the door shut nothing was attempted at all
    def read_pending():
        nonlocal budget, stopped, rate_limited, pending_done, quota_stop
        if pending_done:
            return
        pending_done = True
        for ident in pending[:pending_budget]:
            job = jobs.setdefault(ident, dict(job_id=ident, title='', company='', location='', salary='', work_type='',
                subclassification='', listed_at='', summary='', search_keywords=['(待读取 JD)'],
                url=f'https://au.seek.com/job/{ident}', description='', detail_status='待处理', detail_error='',
                expired=False, collected_at=now))
            if job['detail_status'] == '已读取完整JD':
                continue
            budget -= 1
            outcome = read_detail(fetch, job, errors)
            if outcome == 'quota':      # no share left this hour; hand the slot back and stay owed to it
                budget += 1
                quota_stop = 'detail'
                stopped = True
                break
            if outcome == 'stop':
                stopped = True
                rate_limited = blocked_by(job) or rate_limited
                break
    if (not expanded or backlog_only) and not deferred:   # a cool-down round attempts nothing at all
        read_pending()
    for plan_index in (plan_indices if not stopped and not backlog_only else []):
        keyword, scope = plan[plan_index]
        label = (keyword or '(all ICT)') + (f' [{scope}]' if expanded else '')
        lane = f'{scope}:{keyword}'
        first_page = int(cursor['pages'].get(lane, 1)) if expanded else 1
        per_page = None
        for page in range(first_page, first_page + args.max_pages):
            if expanded and page_budget <= 0:
                break
            page_budget -= 1
            cursor['next'] = (plan_index + 1) % len(plan)
            worktypes = lane_worktypes(keyword, args.worktypes)
            url = search_url(keyword, page, worktypes, scope)
            try:
                cards, total_pages, total_jobs = parse_search(fetch.get(url), keyword, worktypes, scope)
                per_page = per_page or len(cards) or None
                if total_jobs and per_page:   # the pager only shows nearby pages; the count is reliable
                    more = page * per_page < int(total_jobs)
                else:
                    more = total_pages is not None and page < int(total_pages)
                cursor['pages'][lane] = page + 1 if cards and more else 1
                pages.append(dict(keyword=label, scope=scope, page=page, total_pages=total_pages, total_jobs=total_jobs, url=url,
                                  more=bool(cards) and more))
                for card in cards:
                    ident = str(card['id'])
                    if ident in jobs:
                        jobs[ident]['search_keywords'] = sorted(set(jobs[ident]['search_keywords'] + [label]))
                        continue
                    if ident in filtered:
                        continue
                    reason = card_exclusion(card, excluded_subclasses, args.exclude_title)
                    if not reason and scope in ('perth_all', 'remote') and not it_evidence(card.get('title', ''), card.get('teaser', ''), '; '.join(subclasses(card))):
                        reason = '跨分类结果缺少明确 IT 职责证据'
                    if reason:
                        filtered[ident] = dict(job_id=ident, title=card.get('title', ''),
                                               company=card.get('companyName', ''), reason=reason)
                        continue
                    jobs[ident] = dict(job_id=ident, title=card['title'], company=card.get('companyName', ''),
                        location='; '.join(x['label'] for x in card.get('locations', [])),
                        salary=card.get('salaryLabel', ''), work_type='; '.join(card.get('workTypes', [])),
                        subclassification='; '.join(subclasses(card)),
                        listed_at=card.get('listingDate', ''), summary=card.get('teaser', ''),
                        search_keywords=[label], url=f'https://au.seek.com/job/{ident}',
                        description='', detail_status='待处理', detail_error='', expired=False, collected_at=now)
                if expanded:
                    read_pending()
                print(f'{label} page {page}: {len(cards)} cards; {len(jobs)} kept, {len(filtered)} filtered', flush=True)
                page_jobs = sorted((j for j in jobs.values() if j['job_id'] not in known and j['detail_status'] == '待处理'),
                                   key=lambda j: not bool(re.search(r'\b(graduate|junior|entry[- ]level|trainee|interns?|internships?)\b', j['title'], re.I)))
                for job in (page_jobs[:max(budget, 0)] if not stopped else []):
                    budget -= 1
                    outcome = read_detail(fetch, job, errors)
                    if outcome == 'quota':      # the hour's share ran out mid-page; keep that job owed
                        budget += 1
                        quota_stop = 'detail'
                        stopped = True
                        break
                    if outcome == 'stop':
                        stopped = True
                        rate_limited = blocked_by(job) or rate_limited
                        break
                if stopped:
                    break

                if not cards or not more:
                    break
            except QuotaExhausted as exc:       # prevention, not reaction: stop before the edge objects again
                quota_stop = exc.kind
                label = '列表页' if exc.kind == 'list' else '详情页'
                errors.append(f'Search {keyword} page {page}: 本小时{label}请求配额已用完（剩余 {quota.remaining(exc.kind)}），下轮继续')
                stopped = True
                break
            except Exception as exc:
                refused = block_info(exc) if isinstance(exc, urllib.error.HTTPError) else None
                detail = f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else str(exc)
                errors.append(f'Search {keyword} page {page}: {detail}')
                rate_limited = bool(refused) or rate_limited
                stopped = True
                break
        if stopped or (expanded and page_budget <= 0):
            break
    # Prioritize titles matching the user's intended level over sponsored senior ads.
    ordered = sorted(jobs.values(), key=lambda j: not bool(re.search(r'\b(graduate|junior)\b', j['title'], re.I)))
    to_read = [job for job in ordered if job['job_id'] not in known and job['detail_status'] == '待处理']
    for job in ordered:
        if job['job_id'] in known:
            job['detail_status'] = '已入账，未重新读取'
    if stopped and quota_stop:      # an exhausted hourly share is a pause, not a broken listing page
        reason = '未读取（本小时请求配额已用完，下轮继续）'
        interrupted_detail = any(j['detail_status'].startswith('读取失败') for j in jobs.values())
        for job in to_read:
            job['detail_status'] = '未读取（详情采集中断）' if interrupted_detail else reason
    elif stopped:
        interrupted_detail = any(j['detail_status'].startswith('读取失败') for j in jobs.values())
        for job in to_read:
            job['detail_status'] = '未读取（详情采集中断）' if interrupted_detail else '未读取（搜索中断）'
    if not stopped:
        for job in to_read[max(budget, 0):]:
            job['detail_status'] = '未读取（本次数量上限）'
        for job in to_read[:max(budget, 0)]:
            outcome = read_detail(fetch, job, errors)
            if outcome == 'quota':      # the hour ran out while draining the tail; leave it owed, not failed
                budget += 1
                quota_stop = 'detail'
                stopped = True
                break
            if outcome == 'stop':
                stopped = True
                rate_limited = blocked_by(job) or rate_limited
                break
    for job in ordered:
        if job['detail_status'] == '待处理':
            job['detail_status'] = '未读取（详情采集中断）'
    pending_read = sum(jobs[i]['detail_status'] == '已读取完整JD' for i in pending if i in jobs)
    final_pages = {p['keyword']: p for p in pages}
    incomplete = stopped or any(p['more'] for p in final_pages.values()) or (expanded and (len(final_pages) < len(plan) or any(p > 1 for p in cursor['pages'].values())))

    # The backlog is kept outside the ledger on purpose: a listing without a JD never becomes a row, so an
    # interrupted round would otherwise lose those ids forever. A cool-down round leaves the file untouched.
    owed = []
    for job in ordered:                                       # junior titles first, matching how they were promised
        ident = str(job['job_id'])
        if ident in known or job['detail_status'] == '已读取完整JD':
            continue                                          # already held, or read this round: nothing further owed
        if job['detail_status'].startswith('读取失败') and not blocked_by(job):
            continue                                          # an unreadable page must not cost requests forever
        owed.append(ident)                                    # refused, deferred, or simply past this run's cap
    listed = {str(job['job_id']) for job in ordered}          # anything this round never saw at all stays promised
    owed += [ident for ident in pending if ident not in listed]   # earlier promises are honoured before new ones
    queue_written = None                       # None: untouched this round (a cool-down round writes nothing)
    queue_remaining = len(load_queue(queue_path)) if deferred else len(owed)
    if not deferred:
        queue_written = bool(save_queue(queue_path, owed))

    refusal = fetch.block if isinstance(fetch.block, dict) else None
    if refusal is None:
        row = next((j for j in ordered if j.get('block_kind')), None)
        refusal = dict(kind=row['block_kind'], code=row['block_code'], retry_after=None) if row else None
    cooldown_until = block_state.get('until') if deferred else None
    now_utc = datetime.now(timezone.utc)
    if rate_limited and refusal and not deferred:             # name the wait instead of guessing at one
        minutes = CHALLENGE_COOLDOWN_MINUTES if refusal.get('kind') == 'challenge' else THROTTLE_COOLDOWN_MINUTES
        if refusal.get('retry_after'):
            minutes = max(minutes, int(refusal['retry_after'] // 60))
        cooldown_until = (now_utc + timedelta(minutes=minutes)).isoformat()
        save_json(block_path, dict(kind=refusal.get('kind'), code=refusal.get('code'), retry_after=refusal.get('retry_after'),
                                   blocked_at=now_utc.isoformat(), until=cooldown_until, queue=len(owed), transport_used=fetch.transport))
    elif not rate_limited and block_state:                    # a round that got through means the window has passed
        save_json(block_path, {})

    rows = [assess(j) for j in ordered]
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    run_dir = args.output / 'runs' / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    report = dict(run_id=run_id, output_directory=str(run_dir.resolve()), collected_at=now, mode=args.mode,
        keywords=keywords, worktypes=args.worktypes, search_pages=pages, unique_jobs=len(rows),
        card_filtered=len(filtered), card_filtered_jobs=list(filtered.values()),
        known_skipped=sum(job['job_id'] in known for job in rows),
        pending_requested=len(pending), pending_read=pending_read, backlog_only=backlog_only,
        backlog_forced=forced_backlog, queue_local_requested=len(queued_before), queue_written=queue_written,
        queue_remaining=queue_remaining, queue_file=str(queue_path if not deferred else ''),
        detail_deferred=sum(not j["description"] and j["job_id"] not in known for j in rows),
        full_descriptions=sum(bool(j['description']) for j in rows), network_requests=fetch.requests,
        http_attempts=fetch.http_attempts, browser_attempts=fetch.browser_attempts, transport=fetch.transport,
        transport_switched=bool(fetch.switched), browser_available=(fetch.transport == 'chrome') or fetch.can_use_browser(),
        blocked_by=(refusal or {}).get('kind'), block_code=(refusal or {}).get('code'), refusal_label=(BLOCK_LABELS.get((refusal or {}).get('kind')) if refusal else None),
        cooldown_until=cooldown_until, deferred_for_cool_down=bool(deferred), quota_exhausted=quota_stop,
        quota_persisted=bool(quota.persisted), quota_list_remaining=quota.remaining('list'),
        quota_detail_remaining=quota.remaining('detail'),
        max_pages_per_keyword=args.max_pages, max_details=args.max_details, delay_seconds=args.delay,
        jitter_factor=fetch.jitter_factor, hourly_list_limit=quota.limits['list'], hourly_detail_limit=quota.limits['detail'],
        stopped_early=stopped, rate_limited=rate_limited, errors=errors,
        search_truncated=incomplete, search_complete=not incomplete,
        search_plan=[dict(keyword=k, scope=sc) for k, sc in plan], resume_cursor=cursor if expanded else None,
        note='Bounded collection; search totals can vary between page requests. No claim of complete SEEK coverage or confirmed visa eligibility. Cache TTL 6 hours. A refusal writes a cool-down window and keeps unread ids in the local backlog queue; an anti-bot challenge is retried through a real browser first when one exists on this machine. Requests are paced with deterministic jitter and capped per rolling hour.')
    jd_dir = run_dir / 'jd'
    jd_dir.mkdir()
    report['jd_files'] = []
    for job in rows:
        job['jd_file'] = ''
        if job['description']:
            # Metadata comes from this listing, not from the search filter or JD prose.
            filename = hashlib.sha256(job['job_id'].encode()).hexdigest() if not job['job_id'].isdigit() else job['job_id']
            job['jd_file'] = f'jd/{filename}.txt'
            metadata = '\n'.join(f'{label}: {job.get(key, "")}' for label, key in [
                ('Title', 'title'), ('Company', 'company'), ('Listing location', 'location'),
                ('Work type', 'work_type'), ('Source URL', 'url'), ('Collected at (UTC)', 'collected_at')])
            (run_dir / job['jd_file']).write_text(
                '[SEEK listing metadata — separate from employer description]\n' + metadata +
                '\nLocation is the listing label, not a confirmed attendance or remote-work requirement.\n'
                '\n[Employer job description]\n' + job['description'], encoding='utf-8')
            report['jd_files'].append(job['jd_file'])
    (run_dir / 'jobs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    with (run_dir / 'jobs.csv').open('w', encoding='utf-8-sig', newline='') as out:
        fields = list(rows[0]) if rows else ['job_id', 'title', 'detail_status', 'detail_error', 'jd_file']
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: safe_csv(v) for k, v in j.items()} for j in rows)
    (run_dir / 'run_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    # Publish pointer only after the complete output set exists, including empty/failed runs.
    pointer = args.output / f'.latest-{run_id}.tmp'
    pointer.write_text(json.dumps({'run_id': run_id, 'run_directory': f'runs/{run_id}',
                                  'run_report': f'runs/{run_id}/run_report.json'}, indent=2), encoding='utf-8')
    pointer.replace(args.output / 'latest.json')
    if expanded:
        cursor['signature'] = signature
        state_dir.mkdir(parents=True, exist_ok=True)
        temp_cursor = cursor_path.with_suffix('.tmp')
        temp_cursor.write_text(json.dumps(cursor, ensure_ascii=False, indent=2), encoding='utf-8')
        temp_cursor.replace(cursor_path)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 2 if errors else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expanded-search', action='store_true', help='Role searches across Perth categories, plus nationwide remote searches with persisted progress')
    parser.add_argument('--include-remote', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--state-dir', type=Path, default=None, help='Private cache and pagination directory')
    parser.add_argument('--max-pages', type=int, default=1)
    parser.add_argument('--max-details', type=int, default=12)
    parser.add_argument('--delay', type=float, default=2.0)
    parser.add_argument('--impersonate', default=DEFAULT_IMPERSONATE,
                        help='browser TLS fingerprint to present via curl_cffi (e.g. chrome131, safari180, firefox144)')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'seek_output')
    parser.add_argument('--mode', choices=sorted(MODE_KEYWORDS), default='focused',
                        help='focused: graduate/junior/entry level/trainee/internship; broad: all ICT jobs in Perth')
    parser.add_argument('--keywords', type=lambda v: [k.strip() for k in v.split(',') if k.strip()], default=None,
                        help='comma-separated keywords, overriding the mode')
    parser.add_argument('--worktypes', default=WORKTYPES, help='SEEK work type ids (242 full time, 244 contract/temp)')
    parser.add_argument('--exclude-subclass', default=EXCLUDED_SUBCLASSES,
                        help='comma-separated ICT subclassifications skipped before detail requests')
    parser.add_argument('--exclude-title', default=EXCLUDED_TITLE, help='regex; matching titles are skipped')
    parser.add_argument('--known-ids', type=Path, default=None,
                        help='file of job ids already collected; listed but their details are not re-read')
    parser.add_argument('--pending-ids', type=Path, default=None,
                        help='file of job ids known only by title; their details are read first, before any search')
    # Anti-bot handling: transport fallback, cool-down state, local backlog queue, hourly budgets.
    parser.add_argument('--transport', choices=['auto', 'http', 'chrome'], default='auto',
                        help='auto: scripted client until refused, then the local browser; chrome: always use the browser')
    parser.add_argument('--browser-path', default=None, help='path to a Chromium-family browser for --transport chrome')
    parser.add_argument('--profile-dir', type=Path, default=None, help='persistent browser profile kept between rounds (holds the passed check)')
    parser.add_argument('--queue-file', type=Path, default=None, help=f'backlog of unread job ids (default <state-dir>/{QUEUE_FILE})')
    parser.add_argument('--block-file', type=Path, default=None, help=f'refusal and cool-down state (default <state-dir>/{BLOCK_FILE})')
    parser.add_argument('--quota-file', type=Path, default=None, help=f'rolling hourly request record (default <state-dir>/{QUOTA_FILE})')
    parser.add_argument('--max-list-per-hour', type=int, default=MAX_LIST_PER_HOUR, dest='max_list_per_hour',
                        help='search pages per rolling hour; they cost ~1.2 MB each and trigger refusals')
    parser.add_argument('--max-details-per-hour', type=int, default=MAX_DETAILS_PER_HOUR, dest='max_details_per_hour',
                        help='detail pages per rolling hour (~230 KB each); 0 disables details until the next hour')
    parser.add_argument('--jitter-factor', type=float, default=JITTER_FACTOR, dest='jitter_factor',
                        help='requests wait uniform(delay, delay*this); equal gaps read as a script')
    parser.add_argument('--backlog-only', action='store_true', dest='backlog_only',
                        help='drain owed job descriptions with no search-page requests at all (a post-refusal round)')
    args = parser.parse_args()
    if args.max_pages < 1 or args.max_details < 0 or args.delay < 1:
        parser.error('max-pages >= 1, max-details >= 0, delay >= 1 required')
    if args.jitter_factor < 1 or args.max_list_per_hour < 0 or args.max_details_per_hour < 0:
        parser.error('jitter-factor >= 1 and hourly budgets >= 0 required')
    raise SystemExit(run(args))
