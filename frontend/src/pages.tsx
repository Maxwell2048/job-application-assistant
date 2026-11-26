import { useEffect, useMemo, useState } from 'react'
import { api, type Fit, type Job, type ScreeningStarted, type SearchMode, type Task, type TickReport } from './api'
import {
  AnswerPanel, Badge, CONSTRAINT_LABEL, Confirm, Counts, CoverageLine, coverageRank, DocViewer, FAMILY_LABEL, JobCard,
  LEVEL_LABEL, QualityBadge, QualityPanel, REDO_LABEL, RedoButtons, RunTiles, STATUS_LABEL, Stepper, TASK_KIND_LABEL,
  VERDICT_LABEL,
} from './components'
import { useLoader } from './hooks'
import { COLLAPSED_LEVELS, fitLevel, isArchivedJob, isCollapsedLevel } from './jobVisibility'

const MODE_LABEL: Record<SearchMode, string> = { focused: '定向搜索', broad: '扩展搜索' }
const MODE_HINT: Record<SearchMode, string> = {
  focused: '搜索初级和实习关键词（实习另含假期项目），以及开发、数据、BI、测试和支持等岗位；初级关键词限 ICT，数据、开发等岗位可跨分类，但需明确 IT 证据；补充全国远程。只将有完整 JD 的岗位送入筛选。',
  broad: '搜索珀斯 ICT 全分类，补充跨分类相关岗位及全国远程职位。明确高级岗位先排除，混合级别留给 JD 审查；搜索分轮接续，并非每轮覆盖全部。',
}
/** What happens after a JD was handed over for screening. */
function screeningMessage(result: ScreeningStarted): string {
  if (result.screening === 'repost') return '这份 JD 和台账里另一个岗位相同，记为重复发布'
  if (result.screening === 'busy') return '已保存 JD；有一轮采集与筛选正在运行，下一轮会筛选它'
  return '已保存 JD，正在筛选；通过后按“岗位采集”页的设置排队生成或放进“可投 / 待生成”'
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
  { key: '证据不足 / 未生成', statuses: ['low_coverage'] },
]

const LEVEL_ORDER: Record<string, number> = { graduate: 0, junior: 0, unclear: 1, mid: 2, senior: 3 }

/** Order of the "ready" column: level first, then fewer hints (the backend queues by a finer fit rank). */
function readyRank(job: Job): number {
  const fit = job.screening?.rule_result.fit
  if (!fit?.role_family) return 50
  return (LEVEL_ORDER[fit.level] ?? 1) * 10 + Math.min(fit.flags.length, 9)
}

export function Board({ jobs, archivedJobs, onOpen, version, onToast, onChanged }: {
  jobs: Job[]; archivedJobs: Job[]; onOpen: (id: number) => void; version: number; onToast: (t: string) => void; onChanged: () => void
}) {
  const [query, setQuery] = useState('')
  const [showNoJd, setShowNoJd] = useState(false)
  const generateAll = (level: 'default' | 'mid' | 'senior' = 'default') =>
    api.generateReady(level).then((r) => { onToast(`已把 ${r.queued} 个岗位排进生成队列`); onChanged() })
      .catch((exc: Error) => onToast(exc.message))
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? jobs.filter((j) => `${j.title} ${j.company} ${j.location}`.toLowerCase().includes(q)) : jobs
  }, [jobs, query])
  return (
    <>
      <h1>岗位看板</h1>
      <p className="sub">初级岗位在前：每个岗位从被发现到生成材料的位置。“可投 / 待生成”是筛选通过、还没生成材料的初级和未定级岗位；“已生成”按生成日期分组，同一天内按入职条件的证据覆盖排序。中级和高级岗位折叠在页面下方。点卡片看详情、原句和文件。</p>
      <div className="row" style={{ marginTop: 0, marginBottom: 14 }}>
        <input type="text" placeholder="搜索公司、岗位、地点" value={query} onChange={(e) => setQuery(e.target.value)} style={{ maxWidth: 360 }} />
        <span className="msg">{filtered.length} / {jobs.length} 个岗位 · 更新 {version}</span>
      </div>
      <div className="board">
        {COLUMNS.map((col) => {
          const items = filtered.filter((j) => col.statuses.includes(j.status) && (!col.statuses.includes('ready') || !isCollapsedLevel(j)))
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
                <button className="btn mini" style={{ marginBottom: 10 }} onClick={() => generateAll()}>▶ 生成初级和未定级岗位（{items.length}）</button>
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
      <h2 className="level-fold-title">中级和高级岗位<span>默认折叠，点标题展开</span></h2>
      <div className="level-folds">
        {COLLAPSED_LEVELS.map((level) => {
          const items = filtered.filter(j => j.status === 'ready' && fitLevel(j) === level)
          const archived = archivedJobs.filter(j => j.status === 'screened_out' && !j.note?.includes('手动跳过') && fitLevel(j) === level && (!query.trim() || `${j.title} ${j.company} ${j.location}`.toLowerCase().includes(query.trim().toLowerCase())))
          const label = level === 'mid' ? '中级' : '高级'
          return <details className="card level-fold" key={level}>
            <summary>{label}岗位<span>{items.length} 个可投{archived.length > 0 ? ` · ${archived.length} 个已筛掉` : ''}</span></summary>
            <button className="btn mini" disabled={!items.length} onClick={() => generateAll(level)}>生成全部{label}岗位</button>
            {items.map(j => <JobCard key={j.id} job={j} onOpen={onOpen} />)}
            {archived.length > 0 && <details><summary>已筛掉，需逐个查看原因（{archived.length}）</summary>
              <p className="msg">可能还有其他筛选原因；打开详情可自行选择是否生成。</p>
              {archived.map(j => <JobCard key={j.id} job={j} onOpen={onOpen} />)}</details>}
          </details>
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
    <h1>归档岗位</h1>
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
          <h2>任务 {t.id} <Badge kind={t.status}>{STATUS_LABEL[t.status] ?? t.status}</Badge> {TASK_KIND_LABEL[t.kind] && <Badge kind="muted">{t.kind === 'redo' && t.redo ? REDO_LABEL[t.redo] : TASK_KIND_LABEL[t.kind]}</Badge>}</h2>
          <Stepper task={t} />
          {t.error && <p className="msg bad">{t.error}</p>}
          {(t.status === 'queued' || t.status === 'running') && (
            <Confirm text="中止并删除未完成的内容？" onYes={() => act(() => api.cancel(t.id), '已请求中止')}>⏹ 中止</Confirm>
          )}
        </div>
      ))}

      {run && (
        <div className="card" style={{ marginTop: 14 }}>
          <h2>生成结果 · {run.run_id} <QualityBadge quality={run.quality} /></h2>
          {job.status === 'low_coverage' && <p className="msg">自动生成在匹配后停下：入职条件里证据不足的比例过高，只保留了匹配报告。看过报告仍想投，可以继续生成。</p>}
          <Counts counts={run.counts} />
          <CoverageLine coverage={run.coverage} />
          <RunTiles run={run} onOpenDoc={(name) => setDoc({ runId: run.run_id, name })} />
          <RedoButtons run={run} onRedo={(step) => act(() => api.redo(run.run_id, step), `${REDO_LABEL[step]}：已加入队列`)} />
          {run.has_resume && <QualityPanel quality={run.quality} />}
          {Object.keys(run.counts ?? {}).length > 0 && (
            <AnswerPanel run={run} onAsk={(question, words) => api.answer(run.run_id, question, words)
              .then(() => { onToast('回答已加入队列'); reload(); onChanged() })
              .catch((exc: Error) => onToast(exc.message))} />
          )}
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
      <h1>添加岗位 JD</h1>
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
  const [pages, setPages] = useState('')
  const [details, setDetails] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const reloadStatus = status.reload
  const reloadReports = reports.reload
  useEffect(() => {
    const timer = window.setInterval(() => { reloadStatus(); reloadReports() }, 5000)
    return () => window.clearInterval(timer)
  }, [reloadStatus, reloadReports])
  const s = status.data
  const mode: SearchMode = s?.mode ?? 'focused'
  const limits = s?.mode_limits?.[mode]
  const autoGenerate = s?.auto_generate ?? false
  const pageLimit = pages === '' ? limits?.max_pages : Number(pages)
  const detailLimit = details === '' ? limits?.max_details : Number(details)
  const valid = Number.isInteger(pageLimit) && Number(pageLimit) >= 1 && Number(pageLimit) <= 30
    && Number.isInteger(detailLimit) && Number(detailLimit) >= 0 && Number(detailLimit) <= 500
  const disabled = !s || busy || !!s.running
  const change = (action: Promise<unknown>, message: string) => {
    setBusy(true)
    action.then(() => { onToast(message); status.reload(); reports.reload() })
      .catch((exc: Error) => onToast(exc.message)).finally(() => setBusy(false))
  }
  const run = (sources: string[]) => change(api.tick({
    max_pages: pages === '' ? undefined : Number(pages),
    max_details: details === '' ? undefined : Number(details), sources, mode, generate: autoGenerate,
  }), '任务已启动，可在本页查看运行结果。')
  const latest = s?.last_report ?? reports.data?.[0]
  return (
    <div className="collector-page">
      <header className="page-heading"><div><div className="eyebrow">JOB DISCOVERY</div><h1>岗位采集</h1>
        <p className="sub">发现岗位、读取完整 JD，再按你的申请条件筛选。</p></div>
        <span className={`state-pill ${s?.running ? 'running' : ''}`}>{s ? s.running ? '正在采集与筛选' : '等待开始' : '正在连接服务'}</span>
      </header>
      {status.error && <div className="collector-notice danger" role="alert">无法读取运行状态：{status.error}<button className="btn ghost mini" onClick={status.reload}>重新连接</button></div>}
      <div className="collector-layout">
        <section className="card collector-config">
          <div className="section-heading"><span className="section-number">01</span><h2>配置本轮采集</h2></div>
          <fieldset disabled={disabled}><legend>搜索范围 <span className="msg">自动保存</span></legend>
            <div className="choice-grid">{(['focused', 'broad'] as SearchMode[]).map(m => (
              <button key={m} aria-pressed={m === mode} className={`choice ${m === mode ? 'selected' : ''}`}
                onClick={() => m !== mode && change(api.setMode(m), `已切换为${MODE_LABEL[m]}`)}>
                <strong>{MODE_LABEL[m]}</strong><span>{m === 'focused' ? '初级及相关技术岗位' : '更广的 ICT 与相关岗位'}</span>
              </button>))}</div>
            <p className="field-help">{MODE_HINT[mode]}</p>
          </fieldset>
          <fieldset disabled={disabled}><legend>采集预算 <span className="msg">仅对本轮生效</span></legend>
            <div className="choice-grid">
              <label className="budget-field" htmlFor="search-pages"><span>{mode === 'focused' ? '每组关键词搜索页数' : '本轮搜索总页数'}</span>
                <input id="search-pages" type="number" min={1} max={30} step={1} value={pages} placeholder={String(limits?.max_pages ?? '')}
                  onChange={e => setPages(e.target.value)} aria-describedby="pages-help" />
                <small id="pages-help">{mode === 'focused' ? '控制搜索深度，不是岗位数量。' : '控制本轮所有搜索的总页数。'} 默认 {limits?.max_pages ?? '—'} 页</small></label>
              <label className="budget-field" htmlFor="jd-limit"><span>完整 JD 读取上限</span>
                <input id="jd-limit" type="number" min={0} max={500} step={1} value={details} placeholder={String(limits?.max_details ?? '')}
                  onChange={e => setDetails(e.target.value)} aria-describedby="details-help" />
                <small id="details-help">详情请求预算，失败尝试也计入。默认 {limits?.max_details ?? '—'} 份；0 表示不读取新详情。</small></label>
            </div>
            <p className="field-help">留空使用默认值。发现的岗位可能多于 JD 上限；遇到限流可能提前结束，提高上限不保证读取更多。</p>
            {!valid && s && <p className="msg bad" role="alert">搜索页数须为 1–30 的整数，JD 上限须为 0–500 的整数。</p>}
          </fieldset>
          <fieldset disabled={disabled}><legend>筛选通过后 <span className="msg">自动保存</span></legend><div className="choice-grid">
            {[false, true].map(enabled => <button key={String(enabled)} aria-pressed={autoGenerate === enabled}
              className={`choice ${autoGenerate === enabled ? 'selected' : ''}`}
              onClick={() => enabled !== autoGenerate && change(api.setAutoGenerate(enabled), enabled ? '已开启通过后生成简历' : '通过岗位将保存在待生成列表')}>
              <strong>{enabled ? '自动生成简历' : '保存到待生成'}</strong><span>{enabled ? '通过后加入生成队列' : '先查看结果，再手动生成'}</span></button>)}
          </div></fieldset>
          <div className="launch-panel"><p>本轮：<strong>{MODE_LABEL[mode]}</strong> · 最多 <strong>{detailLimit ?? '—'}</strong> 次详情请求 · <strong>{autoGenerate ? '通过后生成简历' : '通过后保存待生成'}</strong></p>
            <button className="btn" disabled={disabled || !valid || !s?.collector_configured} onClick={() => run(['inbox', 'seek'])}>{busy ? '正在提交…' : s?.running ? '本轮正在运行…' : '开始采集与筛选'}</button>
            {s && !s.collector_configured && <p className="msg bad">尚未配置 SEEK 采集器。</p>}
          </div>
        </section>
        <aside className="collector-aside">
          <section className="card"><div className="section-heading"><span className="section-number">02</span><h2>结果会去哪里</h2></div>
            <ol className="flow-list"><li><strong>发现岗位</strong><span>搜索结果只提供候选，不代表已获取 JD。</span></li><li><strong>读取与入库</strong><span>已有 JD 不重复读取；只有标题的岗位不进入看板。</span></li><li><strong>筛选岗位</strong><span>通过的岗位进入 <a href="#/board">岗位看板</a>「可投 / 待生成」；不符合的进入 <a href="#/archive">归档岗位</a>。</span></li><li><strong>生成申请材料</strong><span>{autoGenerate ? '本轮通过的岗位会加入生成队列。' : '本轮不生成简历，可在看板手动选择生成。'}</span></li></ol>
          </section>
          <section className="card"><h2>运行安排</h2><dl className="kv compact"><dt>定时采集</dt><dd>{s ? s.enabled ? `每 ${s.interval_hours} 小时` : '未开启，手动运行' : '—'}</dd><dt>下一轮</dt><dd>{s?.next_run_at ? new Date(s.next_run_at).toLocaleString() : '暂无计划'}</dd></dl>
            <p className="field-help">模型离线时会尝试使用已配置的启动脚本；未恢复时，岗位等待后续模型筛选。</p>
            <details><summary>手动 JD 收件箱</summary><p className="field-help">只读取收件箱中的 JD，不访问 SEEK；也会处理系统内待筛选岗位。通过后遵循上方生成设置。</p><button className="btn ghost" disabled={disabled} onClick={() => run(['inbox'])}>处理收件箱与待筛选岗位</button></details>
          </section>
        </aside>
      </div>
      <section className="card collector-results"><div className="section-heading"><span className="section-number">03</span><h2>最近一轮结果</h2></div>
        {s?.running && <p className="collector-notice" role="status">正在采集或筛选，完成后会自动更新。下方显示最近已完成的一轮。</p>}
        {latest ? <ReportDetail report={latest} /> : <div className="empty-state">尚无采集记录。完成第一轮后，这里会展示发现、读取、入库和筛选的数量。</div>}
      </section>
      <section className="card collector-results"><h2>采集记录</h2>
        {reports.error && <p className="msg bad" role="alert">无法读取记录：{reports.error}</p>}
        <div className="table-scroll"><table><thead><tr><th>开始时间</th><th>搜索模式</th><th>新增入库</th><th>筛选通过</th><th>筛选排除</th><th>运行结果</th><th>报告</th></tr></thead><tbody>
          {(reports.data ?? []).map(r => <tr key={r.id}><td>{new Date(r.started_at).toLocaleString()}</td><td>{r.mode ? MODE_LABEL[r.mode] : '—'}</td><td>{r.new}</td><td>{(r.ready?.length ?? 0) + (r.queued?.length ?? 0) + (r.flagged?.length ?? 0)}</td><td>{r.screened_out?.length ?? 0}</td><td>{r.sources?.seek?.rate_limited ? '采集中断：访问受限' : r.errors?.length ? '有错误，查看报告' : '本轮已完成'}</td><td><button className="btn ghost mini" aria-expanded={open === r.id} onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? '收起' : '展开'}</button></td></tr>)}
          {!reports.data?.length && <tr><td colSpan={7} className="msg">{reports.loading ? '正在读取记录…' : '尚无采集记录'}</td></tr>}
        </tbody></table></div>
        {open && reports.data?.some(r => r.id === open) && <ReportDetail report={reports.data.find(r => r.id === open)!} />}
      </section>
    </div>
  )
}

function ReportDetail({ report }: { report: TickReport }) {
  const section = (title: string, items: TickReport['queued'], bad = false) => {
    if (!items?.length) return null
    const content = items.map(b => <div className="report-job" key={`${b.job_id}-${b.title}`}>
      <a href={`#/jobs/${b.job_id}`}>{b.title}</a><span className="msg">{b.company}</span>
      <span className={`msg ${bad ? 'bad' : ''}`}>{b.reason}</span>
      {b.quote && <div className="quote">“{b.quote}”</div>}
    </div>)
    return bad ? <details className="excluded-results"><summary>{title} · {items.length} 个岗位 <span>查看原因</span></summary>{content}</details>
      : <div style={{ marginTop: 16 }}><h2 style={{ fontSize: 14 }}>{title} · {items.length}</h2>{content}</div>
  }
  const seek = report.sources?.seek
  const known = Number(seek?.known_skipped ?? 0)
  const unread = Number(seek?.detail_deferred ?? seek?.discarded_no_jd ?? 0)
  const sourceErrors = Array.isArray(seek?.errors) ? seek.errors.map(String) : []
  const limited = Boolean(seek?.rate_limited)
  const httpCode = sourceErrors.some(e => /\b429\b/.test(e)) ? 'HTTP 429' : sourceErrors.some(e => /\b403\b/.test(e)) ? 'HTTP 403' : 'HTTP 429 / 403'
  const blockedRaw = seek?.blocked_by
  const blockedBy = blockedRaw === 'challenge' || blockedRaw === 'throttle' ? blockedRaw : null
  const refusalRaw = typeof seek?.refusal_label === 'string' && seek.refusal_label.trim() ? String(seek.refusal_label).trim() : null
  const blockCode = typeof seek?.block_code === 'number' && Number.isFinite(seek.block_code) ? String(Math.trunc(seek.block_code)) : ''
  const deferred = Boolean(seek?.deferred_for_cool_down)
  const cooldownRaw = typeof seek?.cooldown_until === 'string' && seek.cooldown_until ? new Date(seek.cooldown_until) : null
  const cooldownText = cooldownRaw && !Number.isNaN(cooldownRaw.getTime()) ? cooldownRaw.toLocaleString() : null
  const quotaRaw = seek?.quota_exhausted
  const quotaScope = quotaRaw === 'detail' ? '详情页' : quotaRaw === 'list' ? '列表页' : null
  const queueRemaining = typeof seek?.queue_remaining === 'number' && Number.isFinite(seek.queue_remaining) ? Math.max(0, Math.trunc(seek.queue_remaining)) : null
  const transportChrome = seek?.transport === 'chrome'
  const transportSwitched = Boolean(seek?.transport_switched)
  const noBrowserFallback = seek?.browser_available === false && blockedBy != null
  const limitedTitle = deferred
    ? '仍在退避中 · 本轮未发出请求'
    : blockedBy === 'challenge'
      ? `SEEK 访问受限${blockCode ? ` · HTTP ${blockCode}` : ''}（${refusalRaw ?? '反爬校验拦截'}，响应不含重试等待时间）`
      : blockedBy === 'throttle'
        ? `SEEK 访问受限${blockCode ? ` · HTTP ${blockCode}` : ''}（${refusalRaw ?? '请求频率限制'}）`
        : refusalRaw ? `SEEK 访问受限${blockCode ? ` · HTTP ${blockCode}` : ''}（${refusalRaw}）` : `SEEK 访问受限 · ${httpCode}`
  return (
    <div className="run-report">
      <div className="report-meta"><span>{new Date(report.started_at).toLocaleString()}</span><span>用时 {report.seconds ?? '—'} 秒</span><span>模型{report.model_online === null ? '未检测' : report.model_online ? '在线' : '离线'}</span></div>
      {(limited || deferred) && <div className="collector-notice warning" role="status"><strong>{limitedTitle}</strong><p>本轮采集已提前结束，成功读取的 JD 仍会入库并筛选。{cooldownText ? `约至 ${cooldownText} 前不再尝试脚本请求。` : ''}{blockedBy || refusalRaw ? '' : '未读取数量还可能包含预算用尽等情况，不能全部视为限流失败。'}</p></div>}
      {quotaScope && <div className="collector-notice" role="status"><strong>本小时 SEEK 请求配额已用完 · {quotaScope}</strong><p>剩余岗位已存入补抓队列，下轮继续读取；这不是访问受限。</p></div>}
      {seek && !seek.error && <>
        <div className="metric-grid">
          <div className="metric"><span>搜索候选</span><strong>{String(seek.unique_jobs ?? 0)}</strong><small>不含标题 / 分类预先排除</small></div>
          <div className="metric"><span>完整 JD 已读取</span><strong>{String(seek.full_descriptions ?? 0)}</strong><small>可送入入库流程</small></div>
          <div className="metric"><span>已有 JD，跳过读取</span><strong>{known}</strong><small>并非缺失，不重复请求</small></div>
          <div className="metric"><span>尚未取得完整 JD</span><strong>{unread}</strong><small>本轮真正未读的详情，不进入看板</small></div>
        </div>
        {queueRemaining != null && <p className="field-help">补抓队列中 {queueRemaining} 个待读详情（保存在采集器目录，与看板无关）。</p>}
        <p className="field-help">另有 {String(seek.card_filtered ?? 0)} 个按标题 / 分类预先排除。{seek.search_truncated ? '搜索未覆盖全部结果，后续轮次可接续搜索分页；不保证逐一补读本轮所有未读 JD。' : '本轮搜索进度已记录。'}{transportChrome ? '本轮通过本机浏览器读取。' : ''}{transportSwitched ? '脚本请求被拒后已自动切换浏览器继续。' : ''}{noBrowserFallback ? '本机没有可用的浏览器兜底通道。' : ''}</p>
        {Number(seek.pending_requested ?? 0) > 0 && <p className="field-help">历史待补读岗位：本轮已读取 {String(seek.pending_read ?? 0)} / 请求补读 {String(seek.pending_requested)} 个。</p>}
      </>}
      <div className="outcome-strip"><span>新增入库 <b>{report.new}</b></span><span>已有未变 <b>{report.seen}</b></span><span>内容更新 <b>{report.updated}</b></span><span>重复发布 <b>{report.repost}</b></span><span>等待模型 <b>{report.held?.length ?? 0}</b></span></div>
      <p className="field-help">{report.generate === false ? '本轮只采集与筛选；通过岗位保存在看板「可投 / 待生成」，没有自动生成简历。' : '本轮允许筛选通过后自动排队生成材料。'}</p>
      {(seek?.error || sourceErrors.length > 0 || report.errors?.length > 0 || report.notes?.length > 0) && <details className="technical-details"><summary>运行说明与错误详情</summary>
        {seek?.error != null && <p className="msg bad">{String(seek.error)}</p>}
        {[...new Set([...sourceErrors, ...(report.errors ?? [])])].map(n => <p key={n} className="msg bad">{n}</p>)}
        {(report.notes ?? []).map(n => <p key={n} className="msg">{n}</p>)}
      </details>}
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
      <h1>生成记录</h1>
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
      <h1>系统设置</h1>
      <p className="sub">只读。修改请编辑仓库根目录的 <code>.env</code>（变量名以 RAG_ 开头），然后重启后端。</p>
      {error && <p className="msg bad">{error}</p>}
      {data && (
        <div className="card">
          <dl className="kv">
            <dt>简历底稿</dt><dd><code>{data.resume_path}</code></dd>
            <dt>输出目录</dt><dd><code>{data.outputs_dir}</code></dd>
            <dt>JD 目录</dt><dd><code>{data.jobs_dir}</code></dd>
            <dt>数据库</dt><dd>{data.database}</dd>
            <dt>定时采集</dt><dd>{data.autopilot_enabled ? `开启，每 ${data.autopilot_interval_hours} 小时` : '关闭'}</dd>
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
