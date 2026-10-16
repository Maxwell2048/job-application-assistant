import json
import contextlib
import io
import tempfile
import urllib.error
import urllib.parse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from collect_seek import (EXCLUDED_SUBCLASSES, EXCLUDED_TITLE, ROOT, WORKTYPES, assess, card_exclusion,
                          parse_detail, parse_search, plain, search_url, run, safe_csv)


def page(data):
    return '<script>window.SEEK_REDUX_DATA = ' + json.dumps(data) + ';</script>'


class SeekTests(unittest.TestCase):
    def test_changed_filters_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'filter changed'):
            parse_search(page({'search': {'query': {'graduatesearch': 'true'}}}), 'graduate')

    def test_exact_keyword_and_filters(self):
        data = {'search': {'query': {'keywords': '"graduate"', 'classification': '6281',
                'where': 'Perth WA 6000', 'worktype': WORKTYPES}},
                'results': {'results': {'jobs': []}, 'totalPages': 0, 'totalCount': 0}}
        self.assertEqual(parse_search(page(data), 'graduate'), ([], 0, 0))
        self.assertIn('keywords=%22graduate%22', search_url('graduate', 1))
        self.assertIn('keywords=%22entry+level%22', search_url('entry level', 1))
        self.assertNotIn('keywords', search_url('', 1))                      # broad: the whole classification
        self.assertIn('worktype=242%2C244', search_url('', 1))
        broad = dict(data, search={'query': {'classification': '6281', 'where': 'Perth WA 6000', 'worktype': WORKTYPES}})
        self.assertEqual(parse_search(page(broad), ''), ([], 0, 0))
        with self.assertRaisesRegex(ValueError, 'filter changed'):
            parse_search(page(broad), 'junior')

    def test_real_detail(self):
        path = ROOT / 'probe_detail.html'
        if not path.exists():
            self.skipTest('Local probe absent')
        content, expired = parse_detail(path.read_text(encoding='utf-8'), '94690784')
        self.assertGreater(len(content), 1000)
        self.assertNotIn('<p>', content)
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            parse_detail(path.read_text(encoding='utf-8'), '0000')

    def test_pagination_placeholder(self):
        data = {'search': {'query': {'keywords': 'junior', 'classification': '6281',
                'where': 'Perth WA 6000', 'worktype': WORKTYPES}},
                'results': {'results': {'jobs': [{'id': '123'}]}, 'totalPages': 0, 'totalCount': 43}}
        html = page(data) + '<a data-automation="page-2">Next</a>'
        self.assertEqual(parse_search(html, 'junior')[1], 2)
        data['results']['results'] = {'jobs': None, 'isZeroResults': True}
        self.assertEqual(parse_search(page(data), 'junior')[0], [])

    def test_negated_citizenship_not_rejected(self):
        job = dict(title='Junior Developer', description='Australian citizenship is not required. Full working rights required.',
                   location='Perth WA', work_type='Full time')
        self.assertEqual(assess(job)['eligibility_485'], '待核实')
        job['description'] = 'Applicants must be Australian citizens or permanent residents.'
        self.assertIn('限制', assess(job)['eligibility_485'])

    def test_card_exclusion(self):
        subclasses = EXCLUDED_SUBCLASSES.split(',')
        card = lambda title, sub: {'title': title, 'classifications': [{'subclassification': {'description': sub}}]}  # noqa: E731
        self.assertEqual(card_exclusion(card('IT Manager', 'Management'), subclasses, EXCLUDED_TITLE), '子分类 Management')
        self.assertEqual(card_exclusion(card('Senior Developer', 'Developers/Programmers'), subclasses, EXCLUDED_TITLE),
                         '标题含资深或管理级别')
        self.assertEqual(card_exclusion(card('Mid-Level Engineer', 'Engineering - Software'), subclasses, EXCLUDED_TITLE),
                         '标题含资深或管理级别')
        self.assertEqual(card_exclusion(card('Service Desk Analyst', 'Help Desk & IT Support'), subclasses,
                                        EXCLUDED_TITLE), '')

    def test_html_text(self):
        self.assertEqual(plain('<p>A &amp; B</p><script>bad()</script><p>C</p>'), 'A & B\nC')


class RegressionTests(unittest.TestCase):
    def assess_text(self, text, title='Developer'):
        return assess(dict(title=title, description=text, location='Perth WA', work_type='Full time'))

    def test_restriction_with_unrelated_negation(self):
        for text in ['Applicants must be Australian citizens or permanent residents; no visa sponsorship is available.',
                     'Applicants must be Australian citizens and no sponsorship is available.', 'Australian citizens only.']:
            with self.subTest(text=text):
                self.assertIn('限制', self.assess_text(text)['eligibility_485'])

    def test_negation_and_alternative(self):
        for text in ['Australian citizenship is not required.', 'You do not need to be an Australian citizen.',
                     'Applicants must be Australian citizens or hold a valid visa.', 'Australian citizenship is preferred.']:
            with self.subTest(text=text):
                self.assertEqual(self.assess_text(text)['eligibility_485'], '待核实')

    def test_mentoring_and_experience(self):
        job = self.assess_text('You will mentor junior developers. Minimum 5 years of commercial experience required.')
        self.assertIn('缺少初级', job['review_notes'])
        self.assertIn('3年以上', job['review_notes'])
        self.assertEqual(job['priority'], '需要复核')

    def test_entry_role_and_conflicting_experience(self):
        self.assertEqual(self.assess_text('This is an entry-level role.')['priority'], '优先核实')
        self.assertEqual(self.assess_text('This is an entry-level role. Minimum 5 years experience required.')['priority'], '需要复核')

    def test_csv_safety(self):
        for v in ['=1+1', ' +cmd', '-2', '@SUM(A1)', ['=bad', 'ok']]:
            self.assertTrue(safe_csv(v).startswith("'"))


def search_page(url, ids=('123', '456'), cards=None, total=None):
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    query.update(classification='6281', where='Perth WA 6000')
    if cards is None:
        cards = [dict(id=i, title='Junior Developer', companyName='Example', locations=[{'label': 'Perth WA'}],
                      workTypes=['Full time']) for i in ids]
    return page({'search': {'query': query},
                 'results': {'results': {'jobs': cards}, 'totalCount': len(cards) if total is None else total}})


class RunTests(unittest.TestCase):
    def execute(self, root, get, limit=12, **options):
        settings = dict(mode='focused', keywords=['graduate', 'junior'], worktypes=WORKTYPES,
                        exclude_subclass=EXCLUDED_SUBCLASSES, exclude_title=EXCLUDED_TITLE, known_ids=None,
                        pending_ids=None, max_pages=1)
        settings.update(options)
        args = SimpleNamespace(delay=2, refresh=False, max_details=limit, output=root/'output', **settings)
        with patch('collect_seek.ROOT', root), patch('collect_seek.Fetcher.get', side_effect=get), contextlib.redirect_stdout(io.StringIO()):
            code = run(args)
        pointer = json.loads((args.output/'latest.json').read_text())
        folder = args.output/pointer['run_directory']
        return code, folder, json.loads((folder/'jobs.json').read_text(encoding='utf-8')), json.loads((folder/'run_report.json').read_text(encoding='utf-8'))

    @staticmethod
    def success(url):
        if '/job/' in url:
            return page({'jobdetails': {'result': {'job': {'id': url.rsplit('/', 1)[-1], 'content2': '<p>Build applications.</p>', 'isExpired': False}}}})
        return search_page(url)

    def test_isolated_outputs_metadata_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, first, jobs, report = self.execute(root, self.success)
            self.assertEqual(code, 0)
            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0]['search_keywords'], ['graduate', 'junior'])
            content = (first/jobs[0]['jd_file']).read_text(encoding='utf-8')
            for value in ['Listing location: Perth WA', 'Work type: Full time', 'Company: Example', 'not a confirmed attendance']:
                self.assertIn(value, content)
            self.assertTrue(content.endswith('Build applications.'))
            self.assertEqual(len(report['jd_files']), 2)
            _, second, jobs, _ = self.execute(root, self.success, limit=1)
            self.assertNotEqual(first, second)
            self.assertEqual(len(list((first/'jd').glob('*.txt'))), 2)
            self.assertEqual(len(list((second/'jd').glob('*.txt'))), 1)
            self.assertEqual(jobs[1]['detail_status'], '未读取（本次数量上限）')
            self.assertEqual(jobs[1]['jd_file'], '')

    def test_empty_run_replaces_pointer_not_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.execute(root, self.success)
            code, folder, jobs, report = self.execute(root, lambda url: search_page(url, ()))
            self.assertEqual(code, 0)
            self.assertEqual(jobs, [])
            self.assertEqual(report['jd_files'], [])
            self.assertTrue((folder/'jobs.csv').exists())

    def test_search_interrupted(self):
        def get(url):
            if 'keywords=junior' in url:
                raise ValueError('Search challenge')
            return self.success(url)
        with tempfile.TemporaryDirectory() as tmp:
            code, _, jobs, report = self.execute(Path(tmp), get)
            self.assertEqual(code, 2)
            self.assertTrue(report['stopped_early'])
            self.assertTrue(all(j['detail_status'] == '已读取完整JD' for j in jobs))
            self.assertEqual(report['full_descriptions'], 2)

    def test_http_block(self):
        def get(url):
            if '/job/' in url:
                raise urllib.error.HTTPError(url, 429, 'Rate limited', {}, None)
            return self.success(url)
        with tempfile.TemporaryDirectory() as tmp:
            code, _, jobs, report = self.execute(Path(tmp), get)
            self.assertEqual(code, 2)
            self.assertTrue(report['stopped_early'])
            self.assertEqual(jobs[0]['detail_error'], 'HTTP 429')
            self.assertTrue(report['rate_limited'])
            self.assertEqual(jobs[1]['detail_status'], '未读取（详情采集中断）')

    def test_http_error_continues(self):
        def get(url):
            if url.endswith('/123'):
                raise urllib.error.HTTPError(url, 404, 'Not found', {}, None)
            return self.success(url)
        with tempfile.TemporaryDirectory() as tmp:
            code, _, jobs, report = self.execute(Path(tmp), get)
            self.assertEqual(code, 2)
            self.assertFalse(report['stopped_early'])
            self.assertFalse(report['rate_limited'])
            self.assertEqual(jobs[0]['detail_error'], 'HTTP 404')
            self.assertEqual(jobs[1]['detail_status'], '已读取完整JD')

    def test_broad_mode_filters_cards_pages_by_count_and_skips_known(self):
        def card(i, title, sub):
            return dict(id=i, title=title, companyName='Co', locations=[{'label': 'Perth WA'}], workTypes=['Contract/Temp'],
                        classifications=[{'subclassification': {'description': sub}}])
        pages = {1: [card('1', 'Service Desk Analyst', 'Help Desk & IT Support'), card('2', 'IT Manager', 'Management')],
                 2: [card('3', 'Senior Engineer', 'Engineering - Software'), card('4', 'Data Analyst', 'Business/Systems Analysts')],
                 3: [card('5', 'Developer', 'Developers/Programmers')]}
        requested = []

        def get(url):
            requested.append(url)
            if '/job/' in url:
                return self.success(url)
            number = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))['page'])
            self.assertNotIn('keywords', url)
            # the pager only ever shows page 1: the total count decides how far to go
            return search_page(url, cards=pages[number], total=5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'known.txt').write_text('4\n', encoding='utf-8')
            code, _, jobs, report = self.execute(root, get, mode='broad', keywords=None, max_pages=5,
                                                 known_ids=root / 'known.txt')
        self.assertEqual(code, 0)
        self.assertEqual(sorted(j['job_id'] for j in jobs), ['1', '4', '5'])
        self.assertEqual(report['card_filtered'], 2)
        self.assertEqual({j['job_id']: j['reason'] for j in report['card_filtered_jobs']},
                         {'2': '子分类 Management', '3': '标题含资深或管理级别'})
        self.assertEqual(report['known_skipped'], 1)
        self.assertEqual(len([u for u in requested if '/job/' not in u]), 3)
        self.assertEqual(sorted(u.rsplit('/', 1)[-1] for u in requested if '/job/' in u), ['1', '5'])
        known = next(j for j in jobs if j['job_id'] == '4')
        self.assertEqual((known['detail_status'], known['jd_file']), ('已入账，未重新读取', ''))
        self.assertEqual(next(j for j in jobs if j['job_id'] == '1')['subclassification'], 'Help Desk & IT Support')
        self.assertFalse(report['search_truncated'])

    def test_pending_details_are_read_before_any_search(self):
        requested = []

        def detail(ident, title):
            return page({'jobdetails': {'result': {'job': {
                'id': ident, 'content2': '<p>Support our staff.</p>', 'isExpired': False, 'title': title,
                'advertiser': {'name': 'Perth Co'}, 'location': {'label': 'Perth WA'},
                'workTypes': {'label': 'Contract/Temp'}, 'salary': {'label': '$70k'},
                'listedAt': {'dateTimeUtc': '2026-09-26T00:00:00Z'}}}}})

        def get(url):
            requested.append(url)
            if url.endswith('/job/901'):
                return detail('901', 'Service Desk Analyst')
            if url.endswith('/job/902'):
                return detail('902', 'Junior Developer')
            raise urllib.error.HTTPError(url, 429, 'Too Many Requests', {}, None)   # SEEK's budget is spent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pending.txt').write_text('901\n902\n903\n901\n', encoding='utf-8')
            (root / 'known.txt').write_text('903\n', encoding='utf-8')
            code, folder, jobs, report = self.execute(root, get, limit=5, pending_ids=root / 'pending.txt',
                                                      known_ids=root / 'known.txt')
        # the two pending jobs first (903 is known), then the first search page, refused
        self.assertEqual([u.rsplit('/', 1)[-1] for u in requested[:2]], ['901', '902'])
        self.assertIn('keywords=%22graduate%22', requested[2])
        self.assertEqual(len(requested), 3)
        self.assertEqual(code, 2)
        self.assertTrue(report['rate_limited'])
        self.assertEqual((report['pending_requested'], report['pending_read']), (2, 2))
        by_id = {j['job_id']: j for j in jobs}
        self.assertEqual(sorted(by_id), ['901', '902'])
        self.assertEqual(by_id['901']['detail_status'], '已读取完整JD')
        self.assertEqual((by_id['901']['title'], by_id['901']['company'], by_id['901']['work_type']),
                         ('Service Desk Analyst', 'Perth Co', 'Contract/Temp'))
        self.assertTrue(by_id['902']['jd_file'])
        self.assertEqual(len(report['jd_files']), 2)

    def test_pending_details_share_the_detail_budget(self):
        def get(url):
            if '/job/' in url:
                return self.success(url)
            return search_page(url, ids=('123', '901'))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pending.txt').write_text('901\n', encoding='utf-8')
            _, _, jobs, report = self.execute(root, get, limit=1, pending_ids=root / 'pending.txt')
        by_id = {j['job_id']: j for j in jobs}
        self.assertEqual(by_id['901']['detail_status'], '已读取完整JD')      # read once, before the search
        self.assertEqual(by_id['901']['search_keywords'], ['(待读取 JD)', 'graduate', 'junior'])
        self.assertEqual(by_id['123']['detail_status'], '未读取（本次数量上限）')   # the budget of 1 is spent

    def test_failed_run_publishes_empty_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.execute(root, self.success)
            def fail(url):
                raise ValueError('Page changed')
            code, _, jobs, report = self.execute(root, fail)
            self.assertEqual(code, 2)
            self.assertEqual(jobs, [])
            self.assertTrue(report['errors'])


if __name__ == '__main__':
    unittest.main()


class DetailFirstRegressionTests(unittest.TestCase):
    execute = RunTests.execute
    success = staticmethod(RunTests.success)
    def test_search_limit_does_not_consume_all_requests_before_new_details(self):
        requested = []
        def get(url):
            requested.append(url)
            if '/job/' in url:
                return self.success(url)
            number = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))['page'])
            if number == 2:
                raise urllib.error.HTTPError(url, 429, 'Too Many Requests', {}, None)
            return search_page(url, ids=('101', '102'), total=10)
        with tempfile.TemporaryDirectory() as tmp:
            code, folder, jobs, report = self.execute(Path(tmp), get, mode='broad', keywords=None, max_pages=5)
            self.assertEqual(code, 2)
            self.assertEqual(report['full_descriptions'], 2)
            self.assertEqual(len(report['jd_files']), 2)
            self.assertTrue(report['rate_limited'])
            self.assertTrue(report['search_truncated'])
            self.assertTrue(all((folder/j['jd_file']).is_file() for j in jobs))
        self.assertIn('/job/101', requested[1])
        self.assertIn('/job/102', requested[2])
        self.assertIn('page=2', requested[3])


class ExpandedSearchTests(unittest.TestCase):
    execute = RunTests.execute
    success = staticmethod(RunTests.success)

    @staticmethod
    def expanded_page(url, total=3):
        data = json.loads(search_page(url, ids=("101",))[len('<script>window.SEEK_REDUX_DATA = '):-len(';</script>')])
        data['search']['query']['classification'] = ''
        data['search']['query']['where'] = 'All Australia' if 'in-All-Australia' in url else 'Perth WA 6000'
        data['results']['totalCount'] = total
        return page(data)

    def test_expanded_queries_and_remote_validation(self):
        from collect_seek import search_plan
        plan = search_plan('focused', ['graduate', 'data analyst'])
        self.assertIn(('data analyst', 'perth_all'), plan)
        self.assertTrue(any(scope == 'remote' for _, scope in plan))
        self.assertFalse(any(scope == 'remote' for _, scope in search_plan('focused', ['graduate'], False)))
        url = search_url('data analyst remote', 1, scope='remote')
        self.assertIn('/jobs/in-All-Australia?', url)
        cards, _, _ = parse_search(self.expanded_page(url), 'data analyst remote', scope='remote')
        self.assertEqual(len(cards), 1)
        with self.assertRaises(ValueError):
            parse_search(self.expanded_page(url), 'data analyst remote', scope='perth_all')

    def test_pages_resume_and_lanes_rotate_after_rate_limit(self):
        requested=[]
        def get(url):
            requested.append(url)
            if '/job/' in url:
                return self.success(url)
            if 'data+analyst' in url:
                raise urllib.error.HTTPError(url, 429, 'Too Many Requests', {}, None)
            return self.expanded_page(url)
        with tempfile.TemporaryDirectory() as tmp, patch('collect_seek.search_plan', return_value=[('software developer','perth_all'), ('data analyst remote','remote')]):
            root=Path(tmp)
            _, _, _, report=self.execute(root, get, keywords=None, expanded_search=True, limit=0)
            self.assertTrue(report['search_truncated'])
            self.assertFalse(report['search_complete'])
            self.assertEqual(report['resume_cursor']['pages']['perth_all:software developer'], 2)
            requested.clear()
            self.execute(root, get, keywords=None, expanded_search=True, limit=0)
            self.assertIn('keywords=software+developer', requested[0])
            self.assertIn('page=2', requested[0])

    def test_discovery_precedes_backlog_and_shared_budget(self):
        requested=[]
        def get(url):
            requested.append(url)
            return self.success(url) if '/job/' in url else self.expanded_page(url, total=1)
        with tempfile.TemporaryDirectory() as tmp, patch('collect_seek.search_plan', return_value=[('software developer','perth_all')]):
            root=Path(tmp)
            (root/'pending.txt').write_text('901\n',encoding='utf-8')
            _, _, _, report=self.execute(root,get,keywords=None,expanded_search=True,limit=2,pending_ids=root/'pending.txt')
        self.assertNotIn('/job/',requested[0])
        self.assertEqual(sum('/job/' in url for url in requested),2)
        self.assertEqual(report['pending_read'],1)
        self.assertEqual(report['full_descriptions'],2)
        self.assertTrue(report['search_complete'])

    def test_mixed_title_and_technical_sales_survive_cards(self):
        card={'title':'Senior Data Analyst | Data Analyst','classifications':[]}
        self.assertEqual(card_exclusion(card, EXCLUDED_SUBCLASSES.split(','), EXCLUDED_TITLE),'')
        card={'title':'Graduate Solutions Engineer','classifications':[{'subclassification':{'description':'Sales - Pre & Post'}}]}
        self.assertEqual(card_exclusion(card, EXCLUDED_SUBCLASSES.split(','), EXCLUDED_TITLE),'')


class BacklogRecoveryTests(unittest.TestCase):
    execute = RunTests.execute
    success = staticmethod(RunTests.success)
    def test_backlog_uses_entire_budget_without_more_search_cards(self):
        requested=[]
        def get(url):
            requested.append(url)
            return self.success(url)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'pending.txt').write_text('901\n902\n903\n',encoding='utf-8')
            _, _, _, report=self.execute(root,get,keywords=None,expanded_search=True,limit=2,pending_ids=root/'pending.txt')
        self.assertTrue(report['backlog_only'])
        self.assertEqual(report['pending_read'],2)
        self.assertTrue(all('/job/' in url for url in requested))
        self.assertEqual(report['search_pages'],[])
        self.assertFalse(report['search_complete'])

    def test_level_keywords_keep_ict_scope_roles_cross_categories(self):
        from collect_seek import search_plan
        plan=search_plan('focused',['graduate','data analyst'])
        self.assertIn(('graduate','ict'),plan)
        self.assertIn(('data analyst','perth_all'),plan)

    def test_painter_is_excluded_but_technical_industry_role_retained(self):
        self.assertTrue(card_exclusion({'title':'Apprentice Painter'},[],EXCLUDED_TITLE))
        self.assertFalse(card_exclusion({'title':'Healthcare Data Analyst'},[],EXCLUDED_TITLE))


class ItOnlySearchTests(unittest.TestCase):
    def test_ambiguous_roles_and_remote_are_ict_scoped(self):
        from collect_seek import search_plan
        plan=search_plan('focused',['graduate','project coordinator','business analyst','data analyst'])
        self.assertIn(('project coordinator','ict'),plan)
        self.assertIn(('business analyst remote','remote_ict'),plan)
        self.assertIn(('data analyst','perth_all'),plan)
        self.assertIn('/jobs-in-information-communication-technology/in-All-Australia?',search_url('business analyst remote',1,scope='remote_ict'))

    def test_cross_category_noise_is_rejected_before_any_detail(self):
        def get(url):
            self.assertNotIn('/job/',url)
            data=json.loads(search_page(url,ids=('999',))[len('<script>window.SEEK_REDUX_DATA = '):-len(';</script>')])
            data['search']['query']['classification']=''
            data['results']['results']['jobs'][0]['title']='Graduate Accountant'
            data['results']['results']['jobs'][0]['teaser']='Prepare financial accounts.'
            return page(data)
        with tempfile.TemporaryDirectory() as tmp, patch('collect_seek.search_plan',return_value=[('data analyst','perth_all')]):
            _, _, jobs, report=RunTests().execute(Path(tmp),get,keywords=None,expanded_search=True)
        self.assertEqual(jobs,[])
        self.assertEqual(report['card_filtered'],1)
