import { useMemo, useState } from 'react'
import { api, type RunSummary, type Job } from './api'
import { Badge, DocViewer } from './components'
import { useLoader } from './hooks'
import { isArchivedJob } from './jobVisibility'
import { excludeArchivedResumes, groupResumes, type ApplicationFilter } from './resumeGroups'

export function ResumePage({ jobs, version, onToast, onChanged }: {
  jobs: Job[]; version: number; onToast: (text: string) => void; onChanged: () => void
}) {
  const { data, error, loading, reload } = useLoader(() => api.resumes(), [version])
  const [filter, setFilter] = useState<ApplicationFilter>('all')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [doc, setDoc] = useState<{ runId: string; name: string } | null>(null)
  const archivedJobIds = useMemo(() => new Set(jobs.filter(isArchivedJob).map((j) => j.id)), [jobs])
  const runs = useMemo(() => excludeArchivedResumes(data ?? [], archivedJobIds), [data, archivedJobIds])
  const groups = useMemo(() => groupResumes(runs, filter, query), [runs, filter, query])
  const submitted = runs.filter((r) => r.application_status === 'submitted').length
  const change = async (run: RunSummary) => {
    setBusy(run.run_id)
    const value = run.application_status !== 'submitted'
    try {
      await api.setApplication(run.run_id, value)
      onToast(value ? '已标记为已投递' : '已改回未投递')
      reload(); onChanged()
    } catch (exc) { onToast((exc as Error).message) }
    finally { setBusy(null) }
  }
  const card = (r: RunSummary) => {
    const sent = r.application_status === 'submitted'
    const resumeDoc = r.docs.find((d) => d.stem === 'resume_tailored')
    return <article key={r.run_id} className={`resume-card ${sent ? 'submitted' : ''}`}>
      <div className="resume-card-head"><strong>{r.company || r.title || r.jd_file || '简历'}</strong><Badge kind={sent ? 'ok' : 'flag'}>{sent ? '已投递' : '未投递'}</Badge></div>
      {r.company && <p className="msg">{r.title || r.jd_file}</p>}
      <div className="msg">版本 {r.run_id.slice(9, 11)}:{r.run_id.slice(11, 13)} · {r.run_id.slice(-6)}</div>
      {r.submitted_at && <div className="msg">投递标记：{new Intl.DateTimeFormat('zh-CN', { timeZone: 'Australia/Perth', dateStyle: 'short', timeStyle: 'short' }).format(new Date(r.submitted_at))}</div>}
      <div className="row">
        <button className={`btn mini ${sent ? 'ghost' : ''}`} disabled={busy !== null} onClick={() => void change(r)}>{busy === r.run_id ? '保存中…' : sent ? '改为未投递' : '标记已投递'}</button>
        {r.job_id && <a href={`#/jobs/${r.job_id}`}>岗位详情</a>}
        {resumeDoc && <a onClick={() => setDoc({ runId: r.run_id, name: resumeDoc.file.replace(/\.md$/, '') })}>查看简历</a>}
      </div>
      <div className="row">{r.downloads.filter((f) => f.endsWith('.docx')).map((f) => <a key={f} href={api.downloadUrl(r.run_id, f)}>⬇ {f.includes('Cover_Letter') || f.startsWith('cover') ? '求职信' : '简历 Word'}</a>)}</div>
    </article>
  }
  return <>
    <h1>简历投递</h1>
    <p className="sub">按生成日期整理，每个版本单独记录投递状态。投递后手动标记；下载简历不会自动视为已投递。已跳过的岗位及其简历移至<a href="#/archive">已归档岗位</a>。</p>
    <div className="resume-totals"><Badge kind="muted">全部 {runs.length}</Badge><Badge kind="flag">未投递 {runs.length - submitted}</Badge><Badge kind="ok">已投递 {submitted}</Badge></div>
    <div className="row resume-toolbar">
      {([['all', '全部'], ['unsubmitted', '未投递'], ['submitted', '已投递']] as const).map(([key, text]) => <button key={key} className={`btn mini ${filter === key ? '' : 'ghost'}`} aria-pressed={filter === key} onClick={() => setFilter(key)}>{text}</button>)}
      <input aria-label="搜索简历" placeholder="搜索公司或岗位" value={query} onChange={(e) => setQuery(e.target.value)} />
    </div>
    {error && <p className="msg bad">{error} <button className="btn mini" onClick={reload}>重试</button></p>}
    {loading && !data && <p className="msg">加载中…</p>}
    {!loading && !error && groups.length === 0 && <p className="msg">{runs.length ? '没有符合筛选条件的简历。' : '还没有生成完整简历。'}</p>}
    {groups.map(({ date, items }) => {
      const unsent = items.filter((r) => r.application_status !== 'submitted')
      const sent = items.filter((r) => r.application_status === 'submitted')
      return <section key={date} className="resume-date-group">
        <h2>{date} <span className="msg">{items.length} 份 · 未投递 {unsent.length} / 已投递 {sent.length}</span></h2>
        <div className="resume-date-columns">
          {filter !== 'submitted' && <div><h3>未投递 <span>{unsent.length}</span></h3>{unsent.length ? unsent.map(card) : <p className="msg">当天没有未投递简历</p>}</div>}
          {filter !== 'unsubmitted' && <div><h3>已投递 <span>{sent.length}</span></h3>{sent.length ? sent.map(card) : <p className="msg">当天还没有已投递简历</p>}</div>}
        </div>
      </section>
    })}
    {doc && <DocViewer runId={doc.runId} name={doc.name} onClose={() => setDoc(null)} />}
  </>
}
