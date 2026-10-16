"""Collect public SEEK search pages and full job descriptions without login.

Expanded focused mode searches level and role keywords across Perth categories
and nationwide remote roles; broad also includes the whole Perth ICT classification. Both cover the
work types in --worktypes (default full time and contract/temp). Cards whose
subclassification or title rules them out are dropped before any detail page
is fetched, and jobs listed in --known-ids are reported without re-reading
their details.

Expanded searches rotate lanes and persist their next page. A search page is
read before the JD backlog, so discovery is not starved. Stop on 403/429.
Legacy custom keyword searches retain details-first behavior.

Requests present a browser transport-layer (TLS) fingerprint via curl_cffi
(--impersonate) so anti-bot systems do not flag the collector as a script."""
import argparse
import csv
import hashlib
import json
import re
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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

ROLE_KEYWORDS = ['software developer', 'data analyst', 'business intelligence', 'application support', 'service desk', 'test analyst', 'AI engineer', 'data engineer', 'project coordinator', 'machine learning', 'python developer', 'web developer', 'IT support', 'business analyst']
MODE_KEYWORDS = {'focused': ['graduate', 'junior', 'entry level', 'trainee', *ROLE_KEYWORDS], 'broad': ['']}

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
QUOTED = {'graduate', 'entry level'}
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
    term = r'\b(?:graduates?|junior|entry[ -]level)\b'
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
    """Fill one job from its detail page. -> 'ok', 'error' (go on with the next)
    or 'stop' (403/429 or an unknown page: no further requests this run)."""
    try:
        info = detail_info(fetch.get(job['url']), job['job_id'])
    except urllib.error.HTTPError as exc:
        job['detail_status'] = '读取失败（访问拒绝或限流）' if exc.code in (403, 429) else '读取失败（HTTP错误）'
        job['detail_error'] = f'HTTP {exc.code}'
        errors.append(f"JD {job['job_id']}: HTTP {exc.code}")
        return 'stop' if exc.code in (403, 429) else 'error'
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
        if re.search(r'\bgraduate\b|\bjunior\b|entry.level|\d+.{0,8}years.{0,30}experience', s, re.I))
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


class Fetcher:
    def __init__(self, cache, delay, refresh, impersonate=DEFAULT_IMPERSONATE):
        self.cache, self.delay, self.refresh = cache, delay, refresh
        cache.mkdir(parents=True, exist_ok=True)
        self.last = 0
        self.requests = 0
        self.impersonate = impersonate
        self._session = None

    def _open_session(self):
        if self._session is None and _curl_requests is not None:
            self._session = _curl_requests.Session(impersonate=self.impersonate)
        return self._session

    def _fetch(self, url):
        """GET url -> html text. Raises urllib.error.HTTPError on non-2xx so the
        existing 403/429 stop logic is unchanged. Presents the browser TLS
        fingerprint when curl_cffi is available; otherwise the standard library."""
        session = self._open_session()
        if session is not None:
            response = session.get(url, timeout=30)
            if not 200 <= response.status_code < 300:
                raise urllib.error.HTTPError(url, response.status_code, response.reason or 'HTTP error', dict(response.headers), None)
            return response.text
        request = urllib.request.Request(url, headers={'User-Agent': 'SeekJobCollector/0.1 (personal job search)', 'Accept': 'text/html'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode('utf-8')

    def get(self, url):
        dest = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.html')
        if dest.exists() and not self.refresh and time.time() - dest.stat().st_mtime < 21600:
            return dest.read_text(encoding='utf-8')
        time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        self.requests += 1
        html = self._fetch(url)
        state_from_html(html)  # Do not cache challenges or unrelated HTML.
        dest.write_text(html, encoding='utf-8')
        return html


def run(args):
    state_dir = Path(getattr(args, 'state_dir', None) or ROOT)
    fetch = Fetcher(state_dir / '.seek_cache', args.delay, args.refresh, getattr(args, 'impersonate', DEFAULT_IMPERSONATE))
    jobs, pages, errors, filtered = {}, [], [], {}
    now = datetime.now(timezone.utc).isoformat()
    stopped = rate_limited = False
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
    start = int(cursor.get('next', 0)) % len(plan)
    plan_indices = [(start + i) % len(plan) for i in range(len(plan))]
    page_budget = args.max_pages * 4 if args.mode == 'focused' else args.max_pages
    excluded_subclasses = args.exclude_subclass.split(',') if args.exclude_subclass else []
    known = set()
    if args.known_ids:
        known = {line.strip() for line in Path(args.known_ids).read_text(encoding='utf-8').splitlines() if line.strip()}
    pending = []
    if args.pending_ids:
        for line in Path(args.pending_ids).read_text(encoding='utf-8').splitlines():
            ident = line.strip()
            if ident and ident not in known and ident not in pending:
                pending.append(ident)
    backlog_only = expanded and bool(pending) and len(pending) >= max(args.max_details, 1)
    if backlog_only:
        pending_budget_override = args.max_details
    # Details first: the few requests SEEK allows go to jobs already listed, then to search pages.
    budget = args.max_details
    # Reserve detail capacity for discoveries; large JD backlogs cannot monopolize every run.
    pending_budget = min(budget, max(1, budget // 2)) if expanded and budget else budget
    if backlog_only:
        pending_budget = pending_budget_override
    pending_done = False
    def read_pending():
        nonlocal budget, stopped, rate_limited, pending_done
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
            if outcome == 'stop':
                stopped = True
                rate_limited = job['detail_error'] in ('HTTP 403', 'HTTP 429')
                break
    if not expanded or backlog_only:
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
            url = search_url(keyword, page, args.worktypes, scope)
            try:
                cards, total_pages, total_jobs = parse_search(fetch.get(url), keyword, args.worktypes, scope)
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
                                   key=lambda j: not bool(re.search(r'\b(graduate|junior|entry[- ]level|trainee)\b', j['title'], re.I)))
                for job in (page_jobs[:max(budget, 0)] if not stopped else []):
                    budget -= 1
                    outcome = read_detail(fetch, job, errors)
                    if outcome == 'stop':
                        stopped = True
                        rate_limited = rate_limited or job['detail_error'] in ('HTTP 403', 'HTTP 429')
                        break
                if stopped:
                    break

                if not cards or not more:
                    break
            except Exception as exc:
                errors.append(f'Search {keyword} page {page}: {exc}')
                rate_limited = rate_limited or (isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429))
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
    if stopped:
        interrupted_detail = any(j['detail_status'].startswith('读取失败') for j in jobs.values())
        for job in to_read:
            job['detail_status'] = '未读取（详情采集中断）' if interrupted_detail else '未读取（搜索中断）'
    if not stopped:
        for job in to_read[max(budget, 0):]:
            job['detail_status'] = '未读取（本次数量上限）'
        for job in to_read[:max(budget, 0)]:
            outcome = read_detail(fetch, job, errors)
            if outcome == 'stop':
                stopped = True
                rate_limited = rate_limited or job['detail_error'] in ('HTTP 403', 'HTTP 429')
                break
    for job in ordered:
        if job['detail_status'] == '待处理':
            job['detail_status'] = '未读取（详情采集中断）'
    pending_read = sum(jobs[i]['detail_status'] == '已读取完整JD' for i in pending if i in jobs)
    final_pages = {p['keyword']: p for p in pages}
    incomplete = stopped or any(p['more'] for p in final_pages.values()) or (expanded and (len(final_pages) < len(plan) or any(p > 1 for p in cursor['pages'].values())))
    rows = [assess(j) for j in ordered]
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    run_dir = args.output / 'runs' / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    report = dict(run_id=run_id, output_directory=str(run_dir.resolve()), collected_at=now, mode=args.mode,
        keywords=keywords, worktypes=args.worktypes, search_pages=pages, unique_jobs=len(rows),
        card_filtered=len(filtered), card_filtered_jobs=list(filtered.values()),
        known_skipped=sum(job['job_id'] in known for job in rows),
        pending_requested=len(pending), pending_read=pending_read, backlog_only=backlog_only,
        detail_deferred=sum(not j["description"] and j["job_id"] not in known for j in rows),
        full_descriptions=sum(bool(j['description']) for j in rows), network_requests=fetch.requests,
        max_pages_per_keyword=args.max_pages, max_details=args.max_details,
        stopped_early=stopped, rate_limited=rate_limited, errors=errors,
        search_truncated=incomplete, search_complete=not incomplete,
        search_plan=[dict(keyword=k, scope=sc) for k, sc in plan], resume_cursor=cursor if expanded else None,
        note='Bounded collection; search totals can vary between page requests. No claim of complete SEEK coverage or confirmed visa eligibility. Cache TTL 6 hours.')
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
                        help='focused: graduate/junior/entry level/trainee; broad: all ICT jobs in Perth')
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
    args = parser.parse_args()
    if args.max_pages < 1 or args.max_details < 0 or args.delay < 1:
        parser.error('max-pages >= 1, max-details >= 0, delay >= 1 required')
    raise SystemExit(run(args))
