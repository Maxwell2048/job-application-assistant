import { useEffect, useMemo, useState } from 'react'
import { api, type Fit, type Job, type ScreeningStarted, type SearchMode, type Task, type TickReport } from './api'
import {
  Badge, CONSTRAINT_LABEL, Confirm, Counts, CoverageLine, coverageRank, DocViewer, FAMILY_LABEL, JobCard, LEVEL_LABEL,
  RunTiles, STATUS_LABEL, Stepper, VERDICT_LABEL,
} from './components'
import { useLoader } from './hooks'
import { isArchivedJob } from './jobVisibility'

const MODE_LABEL: Record<SearchMode, string> = { focused: '定向：初级及相关技术岗位', broad: '广撒网：ICT、相关岗位及远程' }
const MODE_HINT: Record<SearchMode, string> = {
  focused: '搜索初级关键词，以及开发、数据、BI、测试和支持等岗位；初级关键词限 ICT，数据、开发等岗位可跨分类，但需明确 IT 证据；补充全国远程。JD 积压时先补详情。',
  broad: '搜索珀斯 ICT 全分类，补充跨分类相关岗位及全国远程职位。明确高级岗位先排除，混合级别留给 JD 审查；搜索分轮接续，并非每轮覆盖全部。',
}
/** What happens after a JD was handed over for screening. */
function screeningMessage(result: ScreeningStarted): string {
  if (result.screening === 'repost') return '这份 JD 和台账里另一个岗位相同，记为重复发布'
  if (result.screening === 'busy') return '已保存 JD；有一轮自动处理正在运行，下一轮会筛选它'
  return '已保存 JD，正在筛选；通过后按“自动处理”页的设置排队生成或放进“可投 / 待生成”'
}

function SupplementJd({ job, onDone, onToast }: { job: Job; onDone: () => void; onToast: (t: string) => void }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const save = () => {
    setBusy(true)
    api.supplementJd(job.id, text)
      .then((result) => { onToast(screeningMessage(result)); setText(''); onDone() })
      .catch((exc: Error) => onToast(exc.message))
      .finally(() => setBusy(false))
  }
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <h2>补充 JD</h2>
      <p className="msg" style={{ marginTop: 0 }}>
        这个岗位只有搜索页的标题，SEEK 限流时读不到详情。
        {job.url && <> 在浏览器里<a href={job.url} target="_blank" rel="noreferrer">打开 SEEK 原页</a>，</>}
        复制岗位描述全文粘贴到下面。保存后会像采集到的 JD 一样立即筛选。
      </p>
      <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="把岗位描述全文粘贴到这里……" />
      <div className="row">
        <button className="btn" disabled={busy || text.trim().length < 80} onClick={save}>保存并筛选</button>
        <span className="msg">{text.length} 字符（至少 80）</span>
      </div>
    </div>
  )
}

const DEGREE_LABEL: Record<string, string> = { any_degree: '任何学位', it_related: 'IT 或相关学科', other_field: '指定非 IT 学科' }
const DECISION_LABEL: Record<string, string> = { skip: '不投递', run: '自动生成', run_flagged: '已生成，但请核对' }

const COLUMNS: { key: string; statuses: string[] }[] = [
  { key: '新发现 / 等待', statuses: ['new', 'held'] },
  { key: '可投 / 待生成', statuses: ['ready'] },
  { key: '排队 / 生成中', statuses: ['queued', 'processing'] },
  { key: '已生成', statuses: ['done'] },
  { key: '失败 / 中止', statuses: ['failed', 'cancelled'] },
]

const LEVEL_ORDER: Record<string, number> = { graduate: 0, junior: 0, unclear: 1, mid: 2, senior: 3 }

/** Order of the "ready" column: level first, then fewer hints (the backend queues by a finer fit rank). */
function readyRank(job: Job): number {
  const fit = job.screening?.rule_result.fit
  if (!fit?.role_family) return 50
  return (LEVEL_ORDER[fit.level] ?? 1) * 10 + Math.min(fit.flags.length, 9)
}

export function Board({ jobs, onOpen, version, onToast, onChanged }: {
  jobs: Job[]; onOpen: (id: number) => void; version: number; onToast: (t: string) => void; onChanged: () => void
}) {
  const [query, setQuery] = useState('')
  const [showNoJd, setShowNoJd] = useState(false)
  const generateAll = () =>
    api.generateReady().then((r) => { onToast(`已把 ${r.queued} 个岗位排进生成队列`); onChanged() })
      .catch((exc: Error) => onToast(exc.message))
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? jobs.filter((j) => `${j.title} ${j.company} ${j.location}`.toLowerCase().includes(q)) : jobs
  }, [jobs, query])
  return (
    <>
      <h1>看板</h1>
      <p className="sub">每个岗位从被发现到生成材料的位置。“可投 / 待生成”是筛选通过、还没生成材料的岗位；“已生成”按生成日期分组，同一天内按入职条件的证据覆盖排序。点卡片看详情、原句和文件。</p>
      <div className="row" style={{ marginTop: 0, marginBottom: 14 }}>
        <input type="text" placeholder="搜索公司、岗位、地点" value={query} onChange={(e) => setQuery(e.target.value)} style={{ maxWidth: 360 }} />
        <span className="msg">{filtered.length} / {jobs.length} 个岗位 · 更新 {version}</span>
      </div>
      <div className="board">
        {COLUMNS.map((col) => {
          const items = filtered.filter((j) => col.statuses.includes(j.status))
          if (col.statuses.includes('ready')) items.sort((a, b) => readyRank(a) - readyRank(b) || a.id - b.id)
          const noJd = col.statuses.includes('new') ? filtered.filter((j) => j.status === 'no_jd') : []
          if (col.statuses.includes('done')) {
            // Finished jobs: those a later screening advises against last; then fewest conditions
            // without evidence first, then most with direct evidence.
            const advisedAgainst = (j: Job) => (j.screening?.decision === 'skip' ? 1 : 0)
            items.sort((a, b) => {
              const [ra, rb] = [coverageRank(a.runs[0]?.coverage), coverageRank(b.runs[0]?.coverage)]
              return advisedAgainst(a) - advisedAgainst(b) || ra[0] - rb[0] || ra[1] - rb[1]
            })
          }
          return (
            <div key={col.key} className="column">
              <h3>{col.key}<span>{items.length}</span></h3>
              {col.statuses.includes('ready') && items.length > 0 && (
                <button className="btn mini" style={{ marginBottom: 10 }} onClick={generateAll}>▶ 全部生成（{items.length}）</button>
              )}
              {col.statuses.includes('done') ? <>
                <a className="date-view-link" href="#/resumes">按日期管理投递 →</a>
                {[...new Set(items.map((j) => j.runs.find((r) => r.has_resume)?.generation_date ?? '日期未知'))].sort((a, b) => b.localeCompare(a)).map((date) => <div key={date}>
                  <h4 className="board-date">{date}</h4>
                  {items.filter((j) => (j.runs.find((r) => r.has_resume)?.generation_date ?? '日期未知') === date).map((j) => <JobCard key={j.id} job={j} onOpen={onOpen} />)}
                </div>)}
              </> : items.map((j) => <JobCard key={j.id} job={j} onOpen={onOpen} />)}
              {items.length === 0 && noJd.length === 0 && <p className="msg">—</p>}
              {noJd.length > 0 && (
                <>
                  <button className="btn ghost mini" style={{ margin: '4px 0 10px' }} onClick={() => setShowNoJd(!showNoJd)}>
                    {showNoJd ? '▾' : '▸'} 只有标题、待读取 JD（{noJd.length}）
                  </button>
                  {showNoJd && noJd.map((j) => <JobCard key={j.id} job={j} onOpen={onOpen} />)}
                </>
              )}
            </div>
          )
        })}
      </div>
    </>
  )
}

export function ArchivePage({ jobs, onOpen }: { jobs: Job[]; onOpen: (id: number) => void }) {
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const filtered = jobs.filter((j) => !q || `${j.title} ${j.company} ${j.location}`.toLowerCase().includes(q))
  return <>
    <h1>已归档岗位</h1>
    <p className="sub">跳过、筛掉、过期及受限制的岗位集中保存在这里。点卡片可查看原因、报告和历史记录。</p>
    <div className="row" style={{ marginBottom: 18 }}>
      <input aria-label="搜索归档岗位" type="text" placeholder="搜索公司、岗位、地点" value={query} onChange={(e) => setQuery(e.target.value)} style={{ maxWidth: 360 }} />
      <span className="msg">{filtered.length} / {jobs.length} 个归档岗位</span>
      <a href="#/board">← 返回看板</a>
    </div>
    {filtered.length ? <div className="archive-grid">{filtered.map((j) => <div key={j.id}>
      <Badge kind="muted">{STATUS_LABEL[j.status] ?? j.status}</Badge>
      <JobCard job={j} onOpen={onOpen} />
    </div>)}</div> : <p className="msg">{jobs.length ? '没有符合搜索条件的归档岗位。' : '暂无归档岗位。'}</p>}
  </>
}

export function JobPage({ id, tasks, onToast, refresh, onChanged }: {
  id: number; tasks: Task[]; onToast: (t: string) => void; refresh: number; onChanged: () => void
}) {
  // `refresh` is the parent's version counter: a new value reloads the job, once (useLoader loads on change).
  const { data: job, error, reload } = useLoader(() => api.job(id), [id, refresh])
  const [doc, setDoc] = useState<{ runId: string; name: string } | null>(null)
  if (error) return <p className="msg bad">{error}</p>
  if (!job) return <p className="msg">加载中…</p>
  const live = tasks.filter((t) => t.job_id === job.id)
  const run = job.runs[0]
  const resumeRun = job.runs.find((r) => r.has_resume)
  const archived = isArchivedJob(job)
  const act = (fn: () => Promise<unknown>, done: string) =>
    fn().then(() => { onToast(done); reload(); onChanged() }).catch((exc: Error) => onToast(exc.message))
  return (
    <>
      <a href="#/board">← 看板</a>
      <h1 style={{ marginTop: 8 }}>{job.title}</h1>
      <p className="sub">
        {[job.company, job.location, job.work_type].filter(Boolean).join(' · ')}
        {job.url && <> · <a href={job.url} target="_blank" rel="noreferrer">原始招聘页</a></>}
      </p>
      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
        <div className="card">
          <h2>状态 <Badge kind={job.status}>{STATUS_LABEL[job.status] ?? job.status}</Badge></h2>
          <dl className="kv">
            <dt>来源</dt><dd>{job.source} · {job.external_id}</dd>
            <dt>首次发现</dt><dd>{new Date(job.first_seen).toLocaleString()}</dd>
            <dt>最近出现</dt><dd>{new Date(job.last_seen).toLocaleString()}</dd>
            {job.repost_of && <><dt>重复发布</dt><dd><a href={`#/jobs/${job.repost_of}`}>#{job.repost_of}</a></dd></>}
          </dl>
          {job.note && <pre className="quote" style={{ borderColor: 'var(--accent2)', background: 'rgba(124,140,255,.08)' }}>{job.note}</pre>}
          {resumeRun && <div className="row">
            <Badge kind={resumeRun.application_status === 'submitted' ? 'ok' : 'flag'}>{resumeRun.application_status === 'submitted' ? '已投递' : archived ? '已归档' : '未投递'}</Badge>
            {!archived && <button className="btn ghost mini" onClick={() => act(() => api.setApplication(resumeRun.run_id, resumeRun.application_status !== 'submitted'), resumeRun.application_status === 'submitted' ? '已改回未投递' : '已标记投递')}> {resumeRun.application_status === 'submitted' ? '改为未投递' : '标记已投递'}</button>}
            <a href={archived ? "#/archive" : "#/resumes"}>{archived ? "查看已归档岗位" : "按日期查看全部简历"}</a>
          </div>}
          <div className="row">
            {['new', 'held', 'ready', 'screened_out', 'failed', 'cancelled', 'blocked', 'done'].includes(job.status) && job.jd_path && (
              <button className="btn mini" onClick={() => act(() => api.decide(job.id, job.status === 'done' ? 'rerun' : 'run'), '已加入队列')}>
                {job.status === 'done' ? '重新生成' : '生成材料'}
              </button>
            )}
            {run?.can_interview && <button className="btn ghost mini" onClick={() => act(() => api.interview(job.id), '面试准备已加入队列')}>🎤 生成面试准备</button>}
            {!['screened_out', 'processing', 'queued'].includes(job.status) && (
              <Confirm text="标记为跳过？" onYes={() => act(() => api.decide(job.id, 'skip'), '已跳过')}>跳过</Confirm>
            )}
          </div>
        </div>
        <div className="card">
          <h2>筛选</h2>
          {job.screening ? (
            <>
              <p style={{ margin: '0 0 6px' }}>
                <Badge kind={job.screening.decision === 'skip' ? 'bad' : job.screening.decision === 'run_flagged' ? 'flag' : 'ok'}>
                  {job.screening.rule_result.work_rights?.processing_status === 'error' ? '工作权利筛选失败' : job.screening.model_verdict ? VERDICT_LABEL[job.screening.model_verdict] ?? job.screening.model_verdict : '规则判定'}
                </Badge>
                <span className="msg">{DECISION_LABEL[job.screening.decision] ?? job.screening.decision}</span>
              </p>
              {job.screening.rule_result.work_rights?.reason && <p className="msg">{job.screening.rule_result.work_rights.reason}</p>}
              {job.screening.quote && <div className="quote">“{job.screening.quote}”</div>}
              {(job.screening.rule_result.reasons ?? []).map((r) => <div key={r} className="msg bad">• {r}</div>)}
              {(job.screening.rule_result.flags ?? []).map((r) => <div key={r} className="msg">• {r}</div>)}
              <FitDetails fit={job.screening.rule_result.fit} />
            </>
          ) : <p className="msg">{job.source === 'manual' ? '手动提交的 JD 不经过筛选。' : '尚未筛选。'}</p>}
        </div>
      </div>

      {live.map((t) => (
        <div key={t.id} className="card" style={{ marginTop: 14 }}>
          <h2>任务 {t.id} <Badge kind={t.status}>{STATUS_LABEL[t.status] ?? t.status}</Badge> {t.kind === 'interview' && <Badge kind="muted">面试准备</Badge>}</h2>
          <Stepper task={t} />
          {t.error && <p className="msg bad">{t.error}</p>}
          {(t.status === 'queued' || t.status === 'running') && (
            <Confirm text="中止并删除未完成的内容？" onYes={() => act(() => api.cancel(t.id), '已请求中止')}>⏹ 中止</Confirm>
          )}
        </div>
      ))}

      {run && (
        <div className="card" style={{ marginTop: 14 }}>
          <h2>生成结果 · {run.run_id}</h2>
          <Counts counts={run.counts} />
          <CoverageLine coverage={run.coverage} />
          <RunTiles run={run} onOpenDoc={(name) => setDoc({ runId: run.run_id, name })} />
        </div>
      )}
      {doc && <DocViewer runId={doc.runId} name={doc.name} onClose={() => setDoc(null)} />}

      {!job.jd_path && !['queued', 'processing'].includes(job.status) && (
        <SupplementJd job={job} onToast={onToast} onDone={() => { reload(); onChanged() }} />
      )}

      <div className="card" style={{ marginTop: 14 }}>
        <h2>JD 原文</h2>
        <pre className="jd">{job.jd_text || '（没有 JD 文本）'}</pre>
      </div>
    </>
  )
}

export function SubmitPage({ online, onSubmitted, onScreening, onToast }: {
  online: boolean; onSubmitted: (task: Task) => void; onScreening: (jobId: number, message: string) => void
  onToast: (t: string) => void
}) {
  const [jd, setJd] = useState('')
  const [name, setName] = useState('')
  const [interview, setInterview] = useState(false)
  const [screenFirst, setScreenFirst] = useState(false)
  const [busy, setBusy] = useState(false)
  const submit = () => {
    setBusy(true)
    const done = screenFirst
      ? api.submitForScreening(jd, name).then((result) => { onScreening(result.job_id, screeningMessage(result)); setJd('') })
      : api.submit(jd, name, interview).then((task) => { onSubmitted(task); setJd('') })
    done.catch((exc: Error) => onToast(exc.message)).finally(() => setBusy(false))
  }
  return (
    <>
      <h1>手动提交 JD</h1>
      <p className="sub">粘贴一份招聘广告正文（至少 80 个字符）。默认不经过筛选、直接生成；勾选“先筛选”则像采集到的岗位一样先筛选。</p>
      <div className="card">
        <textarea value={jd} onChange={(e) => setJd(e.target.value)} placeholder="把招聘广告正文粘贴到这里……" />
        <div className="row">
          <input type="text" style={{ maxWidth: 320 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="公司名，英文（用于文件名；留空则从 JD 识别）" />
          <label className="check"><input type="checkbox" checked={screenFirst} onChange={(e) => setScreenFirst(e.target.checked)} /> 先筛选，不直接生成</label>
          {!screenFirst && <label className="check"><input type="checkbox" checked={interview} onChange={(e) => setInterview(e.target.checked)} /> 同时生成面试准备</label>}
          <span className="msg" style={{ marginLeft: 'auto' }}>{jd.length} 字符</span>
        </div>
        <div className="row">
          <button className="btn" disabled={(!online && !screenFirst) || busy || jd.trim().length < 80} onClick={submit}>
            {screenFirst ? '🔍 保存并筛选' : '✨ 生成'}
          </button>
          <span className="msg">{online ? '模型已就绪。' : '模型离线：先启动 llama-server。'}</span>
        </div>
      </div>
    </>
  )
}

export function AutopilotPage({ onToast }: { onToast: (t: string) => void }) {
  const status = useLoader(() => api.autopilot(), [])
  const reports = useLoader(() => api.reports(), [])
  // Empty means "the mode's default"; the placeholders show what that is.
  const [pages, setPages] = useState('')
  const [details, setDetails] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => {
    const timer = window.setInterval(() => { status.reload(); reports.reload() }, 5000)
    return () => window.clearInterval(timer)
  }, [status.reload, reports.reload]) // eslint-disable-line react-hooks/exhaustive-deps
  const s = status.data
  const mode: SearchMode = s?.mode ?? 'focused'
  const limits = s?.mode_limits?.[mode]
  const autoGenerate = s?.auto_generate ?? true
  const run = (sources: string[]) =>
    api.tick({ max_pages: pages ? Number(pages) : undefined, max_details: details ? Number(details) : undefined, sources, mode,
      generate: autoGenerate })
      .then(() => { onToast(`已开始一轮自动处理（${MODE_LABEL[mode]}，${autoGenerate ? '通过后自动生成' : '只搜索和筛选'}）`); status.reload() })
      .catch((exc: Error) => onToast(exc.message))
  const switchGenerate = (enabled: boolean) =>
    api.setAutoGenerate(enabled).then(() => { onToast(enabled ? '筛选通过后自动生成材料' : '只搜索和筛选，通过的岗位放进“可投 / 待生成”'); status.reload() })
      .catch((exc: Error) => onToast(exc.message))
  const switchMode = (next: SearchMode) =>
    api.setMode(next).then(() => { onToast(`已切换为${MODE_LABEL[next]}`); status.reload() })
      .catch((exc: Error) => onToast(exc.message))
  return (
    <>
      <h1>自动处理</h1>
      <p className="sub">每 {s?.interval_hours ?? 6} 小时：确认模型 → 采集 → 入账 → 筛选 → 排队生成。也可以现在手动跑一轮。</p>
      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
        <div className="card">
          <h2>状态</h2>
          {s && (
            <dl className="kv">
              <dt>定时</dt><dd>{s.enabled ? `开启，每 ${s.interval_hours} 小时` : '关闭（RAG_AUTOPILOT_ENABLED=false）'}</dd>
              <dt>正在运行</dt><dd>{s.running ? '是' : '否'}</dd>
              <dt>下一轮</dt><dd>{s.next_run_at ? new Date(s.next_run_at).toLocaleString() : '—'}</dd>
              <dt>SEEK 采集器</dt><dd>{s.collector_configured ? '已配置' : '未配置'}</dd>
              <dt>模型启动脚本</dt><dd>{s.model_launcher_configured ? '已配置' : '未配置'}</dd>
            </dl>
          )}
        </div>
        <div className="card">
          <h2>搜索模式</h2>
          <div className="row" style={{ marginTop: 0 }}>
            {(['focused', 'broad'] as SearchMode[]).map((m) => (
              <button key={m} className={`btn mini ${m === mode ? '' : 'ghost'}`} disabled={!s || !!s.running}
                onClick={() => m !== mode && switchMode(m)}>
                {m === mode ? '● ' : ''}{MODE_LABEL[m]}
              </button>
            ))}
          </div>
          <p className="msg">{MODE_HINT[mode]}定时任务和下面的按钮都用这个模式，重启后保留。</p>
          <h2 style={{ marginTop: 14 }}>筛选通过之后</h2>
          <div className="row" style={{ marginTop: 0 }}>
            <button className={`btn mini ${autoGenerate ? '' : 'ghost'}`} disabled={!s || !!s.running} onClick={() => !autoGenerate && switchGenerate(true)}>
              {autoGenerate ? '● ' : ''}自动排队生成材料
            </button>
            <button className={`btn mini ${autoGenerate ? 'ghost' : ''}`} disabled={!s || !!s.running} onClick={() => autoGenerate && switchGenerate(false)}>
              {autoGenerate ? '' : '● '}只搜索和筛选
            </button>
          </div>
          <p className="msg">只搜索和筛选：采集、入账、筛选照常，通过的岗位放进看板“可投 / 待生成”，不占用模型生成；之后在看板点“全部生成”或逐个“生成材料”。</p>
          <h2 style={{ marginTop: 14 }}>现在跑一轮</h2>
          <div className="row" style={{ marginTop: 0 }}>
            <label className="check">每组搜索页数 <input type="number" min={1} max={30} value={pages} placeholder={String(limits?.max_pages ?? '')}
              onChange={(e) => setPages(e.target.value)} style={{ width: 70 }} /></label>
            <label className="check">详情上限 <input type="number" min={0} max={500} value={details} placeholder={String(limits?.max_details ?? '')}
              onChange={(e) => setDetails(e.target.value)} style={{ width: 80 }} /></label>
          </div>
          <div className="row">
            <button className="btn" disabled={!!s?.running} onClick={() => run(['inbox', 'seek'])}>
              ▶ 采集 + 筛选{autoGenerate ? ' + 排队生成' : '（不生成）'}
            </button>
            <button className="btn ghost" disabled={!!s?.running} onClick={() => run(['inbox'])}>只处理收件箱</button>
          </div>
          <p className="msg" style={{ marginTop: 10 }}>留空即用该模式的默认值。只读取台账里还没有的岗位详情，所以第一轮最慢。模型离线时会先启动它；生成队列在模型离线时暂停等待。</p>
        </div>
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <h2>历史轮次</h2>
        <table>
          <thead><tr><th>开始</th><th>触发</th><th>模式</th><th>采集</th><th>新增</th><th>待生成</th><th>排队</th><th>标记</th><th>筛掉</th><th>等待</th><th>错误</th><th></th></tr></thead>
          <tbody>
            {(reports.data ?? []).map((r) => (
              <tr key={r.id}>
                <td>{new Date(r.started_at).toLocaleString()}</td><td>{r.trigger}</td>
                <td>{r.mode === 'broad' ? '广撒网' : r.mode === 'focused' ? '精准' : '—'}</td><td>{r.collected}</td><td>{r.new}</td><td>{r.ready?.length ?? 0}</td>
                <td>{r.queued?.length ?? 0}</td><td>{r.flagged?.length ?? 0}</td><td>{r.screened_out?.length ?? 0}</td><td>{r.held?.length ?? 0}</td>
                <td className={r.errors?.length ? 'msg bad' : 'msg'}>{r.errors?.length ?? 0}</td>
                <td><button className="btn ghost mini" onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? '收起' : '详情'}</button></td>
              </tr>
            ))}
            {(reports.data ?? []).length === 0 && <tr><td colSpan={12} className="msg">还没有运行记录</td></tr>}
          </tbody>
        </table>
        {open && reports.data && <ReportDetail report={reports.data.find((r) => r.id === open)!} />}
      </div>
    </>
  )
}

function ReportDetail({ report }: { report: TickReport }) {
  const section = (title: string, items: TickReport['queued'], bad = false) => items?.length ? (
    <div style={{ marginTop: 12 }}>
      <h2 style={{ fontSize: 14 }}>{title} · {items.length}</h2>
      {items.map((b) => (
        <div key={`${b.job_id}-${b.title}`} style={{ marginBottom: 6 }}>
          <a href={`#/jobs/${b.job_id}`}>{b.title}</a> <span className="msg">{b.company}</span>
          <span className={`msg ${bad ? 'bad' : ''}`}> — {b.reason}</span>
          {b.quote && <div className="quote">“{b.quote}”</div>}
        </div>
      ))}
    </div>
  ) : null
  const seek = report.sources?.seek as Record<string, unknown> | undefined
  return (
    <div style={{ marginTop: 14 }}>
      <p className="msg">模型 {report.model_online ? '在线' : '离线'} · 用时 {report.seconds}s · 已见 {report.seen} · 更新 {report.updated} · 重复 {report.repost} · 缺 JD {report.no_jd}</p>
      {seek && !seek.error && (
        <p className="msg">
          SEEK：发现 {String(seek.unique_jobs ?? 0)} 个，读取新详情 {String(seek.full_descriptions ?? 0)} 份，
          已入账跳过 {String(seek.known_skipped ?? 0)} 个，按子分类或标题先行排除 {String(seek.card_filtered ?? 0)} 个
          {Number(seek.pending_requested ?? 0) > 0 ? `；先补读只有标题的岗位 ${String(seek.pending_read ?? 0)} / ${String(seek.pending_requested)} 份` : ''}
          {Number(seek.discarded_no_jd ?? 0) > 0 ? `；${String(seek.discarded_no_jd)} 个无完整 JD 结果未入账` : ''}
          {seek.backlog_only ? '；本轮优先补全积压 JD，暂停发现新标题' : seek.search_truncated ? '；搜索尚未覆盖全部结果，下轮接续' : ''}
        </p>
      )}
      {(report.notes ?? []).map((n) => <div key={n} className="msg">• {n}</div>)}
      {(report.errors ?? []).map((n) => <div key={n} className="msg bad">• {n}</div>)}
      {report.generate === false && <p className="msg">这一轮只搜索和筛选，没有排队生成。</p>}
      {section('可投 / 待生成', report.ready ?? [])}
      {section('已排队生成', report.queued)}
      {section('已生成但需核对', report.flagged)}
      {section('已筛掉', report.screened_out, true)}
      {section('等待模型', report.held)}
    </div>
  )
}

export function RunsPage() {
  const { data, error } = useLoader(() => api.runs(), [])
  const [doc, setDoc] = useState<{ runId: string; name: string } | null>(null)
  return (
    <>
      <h1>历史运行</h1>
      <p className="sub">outputs/ 下的全部运行目录，包括自动化上线之前的。</p>
      {error && <p className="msg bad">{error}</p>}
      <div className="card">
        <table>
          <thead><tr><th>运行</th><th>JD</th><th>状态</th><th>判断</th><th>文档</th><th>Word</th></tr></thead>
          <tbody>
            {(data ?? []).map((r) => (
              <tr key={r.run_id}>
                <td><code>{r.run_id}</code></td>
                <td style={{ maxWidth: 260, wordBreak: 'break-all' }}>{r.jd_file}</td>
                <td><Badge kind={r.run_status === 'complete' ? 'ok' : r.run_status === 'partial' ? 'flag' : 'bad'}>{r.run_status ?? '?'}</Badge></td>
                <td>{['direct', 'related', 'insufficient'].map((k) => <Badge key={k} kind={k}>{k === 'direct' ? '直接' : k === 'related' ? '相关' : '不足'} {r.counts[k] ?? 0}</Badge>)}</td>
                <td>{r.docs.map((d) => <a key={d.stem} style={{ marginRight: 8 }} onClick={() => setDoc({ runId: r.run_id, name: d.file.replace(/\.md$/, '') })}>{d.label}</a>)}</td>
                <td>{r.downloads.filter((f) => f.endsWith('.docx')).map((f) => <a key={f} style={{ display: 'block' }} href={api.downloadUrl(r.run_id, f)}>⬇ {f.includes('Cover_Letter') || f.startsWith('cover') ? 'Cover Letter' : 'Resume'}</a>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {doc && <DocViewer runId={doc.runId} name={doc.name} onClose={() => setDoc(null)} />}
    </>
  )
}

export function SettingsPage() {
  const { data, error } = useLoader(() => api.settings(), [])
  return (
    <>
      <h1>设置</h1>
      <p className="sub">只读。修改请编辑仓库根目录的 <code>.env</code>（变量名以 RAG_ 开头），然后重启后端。</p>
      {error && <p className="msg bad">{error}</p>}
      {data && (
        <div className="card">
          <dl className="kv">
            <dt>简历底稿</dt><dd><code>{data.resume_path}</code></dd>
            <dt>输出目录</dt><dd><code>{data.outputs_dir}</code></dd>
            <dt>JD 目录</dt><dd><code>{data.jobs_dir}</code></dd>
            <dt>数据库</dt><dd>{data.database}</dd>
            <dt>定时自动处理</dt><dd>{data.autopilot_enabled ? `开启，每 ${data.autopilot_interval_hours} 小时` : '关闭'}</dd>
            <dt>SEEK 采集器</dt><dd><code>{data.collector_dir ?? '未配置'}</code></dd>
            <dt>模型启动脚本</dt><dd><code>{data.model_launcher ?? '未配置'}</code></dd>
          </dl>
        </div>
      )}
    </>
  )
}

function FitDetails({ fit }: { fit?: Fit | null }) {
  if (!fit) return null
  const quote = (text?: string) => (text ? <div className="quote">“{text}”</div> : null)
  return (
    <>
      <h2 style={{ marginTop: 14 }}>匹配度预筛</h2>
      {fit.role_family ? (
        <>
          <p style={{ margin: '0 0 6px' }}>
            <Badge kind={fit.level === 'mid' || fit.level === 'senior' ? 'flag' : 'ok'}>{LEVEL_LABEL[fit.level] ?? fit.level}</Badge>
            <Badge kind="muted">{FAMILY_LABEL[fit.role_family] ?? fit.role_family}</Badge>
            <span className="msg">{fit.reason}</span>
          </p>
          {quote(fit.level_quote)}
          {(fit.min_years ?? 0) > 0 && fit.years_quoted && (
            <div className="msg">
              经验：{fit.min_years} 年以上{fit.years_mandatory ? '（硬性）' : '（非硬性）'}
              {fit.years_quote !== fit.level_quote && quote(fit.years_quote)}
            </div>
          )}
          {(fit.required_stack ?? []).length > 0 && (
            <div className="msg">
              必需技术：{(fit.required_stack ?? []).map((t) => (
                <Badge key={t} kind={(fit.missing_stack ?? []).includes(t) ? 'flag' : 'ok'}>{t}</Badge>
              ))}
              {fit.known_stack_size === 0 && '（未找到个人技术栈，未比对）'}
            </div>
          )}
          {fit.degree && fit.degree !== 'unspecified' && (
            <div className="msg">学历：{DEGREE_LABEL[fit.degree] ?? fit.degree}{quote(fit.degree_quote)}</div>
          )}
          {(fit.constraints ?? []).map((c) => (
            <div key={c.kind + c.quote} className="msg">{CONSTRAINT_LABEL[c.kind] ?? c.kind}{quote(c.quote)}</div>
          ))}
        </>
      ) : null}
      {fit.skips.map((r) => <div key={r} className="msg bad">• {r}</div>)}
      {fit.flags.map((r) => <div key={r} className="msg">⚑ {r}</div>)}
    </>
  )
}
