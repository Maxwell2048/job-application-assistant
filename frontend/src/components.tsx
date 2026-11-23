import { isArchivedJob } from './jobVisibility'
import { useState, type ReactNode } from 'react'
import { api, type Coverage, type Job, type Quality, type RedoStep, type RunSummary, type Task } from './api'
import { useLoader } from './hooks'

export const STATUS_LABEL: Record<string, string> = {
  new: '新发现', no_jd: '待读取 JD', held: '等待筛选', ready: '待生成', queued: '排队中', processing: '生成中', done: '已生成',
  failed: '失败', blocked: '地点不符', cancelled: '已中止', screened_out: '已筛掉', expired: '已过期',
  low_coverage: '证据不足',
}

/** What a task of each kind does, for task cards. */
export const TASK_KIND_LABEL: Record<string, string> = { interview: '面试准备', answer: '回答申请问题', redo: '部分重做' }
export const REDO_LABEL: Record<RedoStep, string> = {
  tailor: '继续生成简历', closing: '重写摘要和求职信', assemble: '重新组装简历', export: '重新导出 Word',
}

const QUALITY_TEXT = { ok: '质量通过', warn: '提醒', bad: '问题' } as const

/** Short quality marker for cards and lists. */
export function QualityBadge({ quality }: { quality?: Quality | null }) {
  if (!quality) return null
  const bad = quality.checks.filter((c) => c.level === 'bad').length
  const warn = quality.checks.filter((c) => c.level === 'warn').length
  const text = quality.level === 'ok' ? QUALITY_TEXT.ok : [bad ? `${bad} 项问题` : '', warn ? `${warn} 项提醒` : ''].filter(Boolean).join(' · ')
  return <Badge kind={quality.level === 'ok' ? 'ok' : quality.level === 'warn' ? 'flag' : 'bad'}>{text}</Badge>
}

/** The checks of a run's quality report, problems first, and the model's time and retries. */
export function QualityPanel({ quality }: { quality?: Quality | null }) {
  if (!quality) return <p className="msg">还没有质量报告（新生成或重新导出后自动生成）。</p>
  const order = { bad: 0, warn: 1, ok: 2 }
  const checks = [...quality.checks].sort((a, b) => order[a.level] - order[b.level])
  const llm = quality.llm
  return (
    <div style={{ marginTop: 10 }}>
      <h3 style={{ margin: '0 0 6px' }}>投递前检查 <QualityBadge quality={quality} /></h3>
      {checks.map((c) => (
        <div key={c.key} className={`msg ${c.level === 'bad' ? 'bad' : c.level === 'ok' ? 'ok' : ''}`}>
          {c.level === 'ok' ? '✓' : c.level === 'warn' ? '⚑' : '✗'} {c.label}{c.detail ? `：${c.detail}` : ''}
        </div>
      ))}
      {llm?.calls ? (
        <div className="msg" style={{ marginTop: 6 }} title={Object.entries(llm.by_task ?? {}).map(([k, v]) => `${k}: ${v.calls} 次 · ${v.seconds}s · 重试 ${v.retries}`).join('\n')}>
          模型调用 {llm.calls} 次 · 共 {Math.round((llm.seconds ?? 0) / 60 * 10) / 10} 分钟 · 重试 {llm.retries ?? 0} 次{llm.failed ? ` · 失败 ${llm.failed} 次` : ''}
          {llm.failure_types && Object.keys(llm.failure_types).length > 0 && `（${Object.entries(llm.failure_types).map(([k, v]) => `${k} ${v}`).join('，')}）`}
        </div>
      ) : null}
    </div>
  )
}

/** Answers to application-form questions, written from the run's verified material. */
export function AnswerPanel({ run, onAsk }: { run: RunSummary; onAsk: (question: string, words: number) => Promise<unknown> }) {
  const [question, setQuestion] = useState('')
  const [words, setWords] = useState(250)
  const [busy, setBusy] = useState(false)
  const ask = () => {
    setBusy(true)
    onAsk(question.trim(), words).then(() => setQuestion('')).finally(() => setBusy(false))
  }
  const copy = (text: string) => { void navigator.clipboard?.writeText(text) }
  return (
    <div style={{ marginTop: 14 }}>
      <h3 style={{ margin: '0 0 6px' }}>申请表问题</h3>
      <p className="msg">粘贴申请表里的问题（如 “What relevant skills and experience would you bring to this role?”），按本岗位的已核对材料写英文回答；需要你本人确认的事实单独列出，不写成已具备。</p>
      <textarea style={{ minHeight: 70 }} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="申请表问题……" />
      <div className="row">
        <label className="msg">字数上限 <input type="number" min={40} max={600} step={10} value={words} style={{ width: 90 }}
          onChange={(e) => setWords(Number(e.target.value) || 250)} /></label>
        <button className="btn mini" disabled={busy || question.trim().length < 5} onClick={ask}>生成回答</button>
      </div>
      {(run.answers ?? []).map((a) => {
        const text = (a.answer ?? []).map((p) => p.text).join('\n\n')
        return (
          <div key={a.id} className="card" style={{ marginTop: 10 }}>
            <div className="msg"><strong>{a.question}</strong> · {a.created_at.replace('T', ' ')}{a.words ? ` · ${a.words} 词` : ''}</div>
            {a.status === 'complete' ? <>
              <pre className="quote" style={{ borderColor: 'var(--accent)', background: 'rgba(57,208,200,.06)' }}>{text}</pre>
              <button className="btn ghost mini" onClick={() => copy(text)}>复制回答</button>
              {(a.to_confirm ?? []).length > 0 && <div className="msg" style={{ marginTop: 6 }}>
                需要你确认后再补充：{(a.to_confirm ?? []).join('；')}</div>}
            </> : <p className="msg bad">生成失败：{a.error}</p>}
          </div>
        )
      })}
    </div>
  )
}

/** Partial redo of a finished run, without matching the JD again. */
export function RedoButtons({ run, onRedo }: { run: RunSummary; onRedo: (step: RedoStep) => void }) {
  const steps: RedoStep[] = run.has_resume
    ? ['closing', 'assemble', 'export']
    : run.has_suggestions ? ['assemble'] : run.counts && Object.keys(run.counts).length ? ['tailor'] : []
  if (!steps.length) return null
  return (
    <div className="row">
      {steps.map((step) => (
        <button key={step} className={`btn mini ${step === 'tailor' ? '' : 'ghost'}`} onClick={() => onRedo(step)}
          title={step === 'export' ? '不调用模型，只按当前规则重新生成 Word' : '沿用已有的匹配结果，不重新匹配 JD'}>
          {REDO_LABEL[step]}
        </button>
      ))}
    </div>
  )
}
export const VERDICT_LABEL: Record<string, string> = {
  citizen_or_pr: '要求公民或 PR', clearance: '要求安全许可', full_rights_ok: '要求完整工作权利（485 满足）',
  no_sponsorship_ok: '不提供担保（485 满足）', unspecified: '未提及工作权利', unclear: '工作权利存疑',
}
export const LEVEL_LABEL: Record<string, string> = { graduate: '毕业生', junior: '初级', mid: '中级', senior: '高级', unclear: '级别未说明' }
export const FAMILY_LABEL: Record<string, string> = {
  software_dev: '软件开发', data_ai: '数据与 AI', it_support: 'IT 支持', infrastructure: '系统与网络',
  security: '网络安全', business_analysis: '业务分析与设计', non_technical: '非技术岗位',
}
export const CONSTRAINT_LABEL: Record<string, string> = {
  driver_licence: '驾照或自备车辆', wwcc: 'Working with Children Check', shift_work: '轮班或 on-call',
  fixed_term: '固定期限或合同岗位', other: '其他硬性条件',
}

/** Share of conditions without evidence, then share with direct evidence: the board's order for finished jobs. */
export function coverageRank(coverage?: Coverage): [number, number] {
  const total = coverage?.total ?? 0
  if (!total) return [2, 0] // no coverage yet: after every job that has one
  return [((coverage?.insufficient ?? 0) + (coverage?.error ?? 0)) / total, -(coverage?.direct ?? 0) / total]
}

const COUNT_LABEL: Record<string, string> = { direct: '直接支持', related: '相关但不足', insufficient: '证据不足', error: '未处理' }
const DOC_ICON: Record<string, string> = { report: '🔍', cv_suggestions: '✍️', resume_tailored: '📄', interview_prep: '🎤' }

export function Badge({ kind, children }: { kind: string; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>
}

export function Stepper({ task }: { task: Task }) {
  return (
    <div className="stepper">
      {task.stages.map((s) => (
        <div key={s.key} className={`step ${s.status}`} title={s.message}>
          {s.label}
          <span className="m">{[s.seconds != null ? `${s.seconds}s` : '', s.message].filter(Boolean).join(' · ')}</span>
        </div>
      ))}
    </div>
  )
}

export function Counts({ counts }: { counts: Record<string, number> }) {
  const keys = ['direct', 'related', 'insufficient', 'error']
  if (!keys.some((k) => k in counts)) return null
  return (
    <div className="stats">
      {keys.map((k) => (
        <div key={k} className={`stat ${k}`}>
          <div className="v">{counts[k] ?? 0}</div>
          <div className="k">{COUNT_LABEL[k]}</div>
        </div>
      ))}
    </div>
  )
}

export function RunTiles({ run, onOpenDoc }: { run: RunSummary; onOpenDoc: (name: string) => void }) {
  return (
    <div className="tiles">
      {run.docs.map((d) => (
        <a key={d.stem} className="tile" onClick={() => onOpenDoc(d.file.replace(/\.md$/, ''))}>
          <div className="ic">{DOC_ICON[d.stem] ?? '📁'}</div>
          <div className="t">{d.label}</div>
        </a>
      ))}
      {run.downloads.filter((f) => f.endsWith('.docx')).map((f) => (
        <a key={f} className="tile dl" href={api.downloadUrl(run.run_id, f)}>
          <div className="ic">⬇️</div>
          <div className="t">{f.includes('Cover_Letter') || f.startsWith('cover_letter') ? 'Cover Letter.docx' : 'Resume.docx'}</div>
          <div className="msg" style={{ fontSize: 11 }}>{f}</div>
        </a>
      ))}
    </div>
  )
}

export function DocViewer({ runId, name, onClose }: { runId: string; name: string; onClose: () => void }) {
  const { data, error } = useLoader(() => api.doc(runId, name), [runId, name])
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="row" style={{ marginTop: 0, justifyContent: 'space-between' }}>
        <h2 style={{ margin: 0 }}>{name} · {runId}</h2>
        <button className="btn ghost mini" onClick={onClose}>关闭</button>
      </div>
      {error && <p className="msg bad">{error}</p>}
      {data && <div className="doc" dangerouslySetInnerHTML={{ __html: data.html }} />}
    </div>
  )
}

export function CoverageLine({ coverage }: { coverage?: Coverage }) {
  if (!coverage?.total) return null
  return (
    <div className="r" style={{ color: 'var(--muted)' }} title="入职条件（不含职责和加分项）的证据覆盖，不是录用概率">
      条件 {coverage.total}：直接 {coverage.direct ?? 0} / 相关 {coverage.related ?? 0} / 不足 {(coverage.insufficient ?? 0) + (coverage.error ?? 0)}
      {(coverage.hard_gaps ?? 0) > 0 && <span style={{ color: 'var(--bad)' }}> · 学历/证照/年限缺口 {coverage.hard_gaps}</span>}
    </div>
  )
}

export function JobCard({ job, onOpen }: { job: Job; onOpen: (id: number) => void }) {
  const screening = job.screening
  const fit = screening?.rule_result.fit
  const fitSkipped = screening?.decision === 'skip' && fit?.decision === 'skip'
  const reason = fitSkipped
    ? fit.skips.join('；')
    : screening?.decision === 'skip'
      ? (screening.model_verdict ? VERDICT_LABEL[screening.model_verdict] : (screening.rule_result.reasons ?? []).join('；'))
      : screening?.decision === 'run_flagged'
        ? `⚠ ${fit?.processing_status === 'error' || fit?.flags.some((flag) => flag.startsWith('匹配度预筛失败')) ? '匹配度预筛失败' : screening.model_verdict === 'unclear' ? screening.rule_result.work_rights?.processing_status === 'error' ? '工作权利筛选失败' : '工作权利存疑' : '有条件需核对'}`
        : ''
  const counts = job.runs[0]?.counts
  return (
    <a className="jobcard" onClick={() => onOpen(job.id)}>
      <div className="t">{job.title || '(无标题)'}</div>
      <div className="c">{[job.company, job.location, job.source === 'seek' ? 'SEEK' : job.source === 'inbox' ? '收件箱' : '手动'].filter(Boolean).join(' · ')}</div>
      {fit?.role_family && (
        <div style={{ marginTop: 6 }}>
          <Badge kind={fit.level === 'mid' || fit.level === 'senior' ? 'flag' : fit.level === 'unclear' ? 'muted' : 'ok'}>{LEVEL_LABEL[fit.level] ?? fit.level}</Badge>
          <Badge kind="muted">{FAMILY_LABEL[fit.role_family] ?? fit.role_family}</Badge>
          {!fitSkipped && fit.flags.length > 0 && <Badge kind="flag">⚑ {fit.flags.length} 项提示</Badge>}
        </div>
      )}
      {job.runs.find((r) => r.has_resume) && <div style={{ marginTop: 8 }}><Badge kind={job.runs.find((r) => r.has_resume)?.application_status === 'submitted' ? 'ok' : 'flag'}>{job.runs.find((r) => r.has_resume)?.application_status === 'submitted' ? '已投递' : isArchivedJob(job) ? '已归档' : '未投递'}</Badge>
        <QualityBadge quality={job.runs.find((r) => r.has_resume)?.quality} /></div>}
      {job.status === 'low_coverage' && <div className="r" style={{ color: 'var(--warn)' }}>证据覆盖不足，已停在匹配报告；打开详情可继续生成</div>}
      {reason && <div className={`r ${screening?.decision === 'skip' ? 'bad' : ''}`}>{reason}</div>}
      {job.status === 'no_jd' && <div className="r" style={{ color: 'var(--muted)' }}>只有搜索页标题，下一轮采集读取 JD 后再筛选</div>}
      {job.repost_of && <div className="r bad">重复发布，见 #{job.repost_of}</div>}
      <CoverageLine coverage={job.runs[0]?.coverage} />
      {!job.runs[0]?.coverage?.total && counts && Object.keys(counts).length > 0 && (
        <div style={{ marginTop: 6 }}>
          <Badge kind="direct">直接 {counts.direct ?? 0}</Badge>
          <Badge kind="related">相关 {counts.related ?? 0}</Badge>
          <Badge kind="insufficient">不足 {counts.insufficient ?? 0}</Badge>
        </div>
      )}
    </a>
  )
}

export function Confirm({ text, onYes, children }: { text: string; onYes: () => void; children: ReactNode }) {
  const [asking, setAsking] = useState(false)
  if (!asking) return <button className="btn danger mini" onClick={() => setAsking(true)}>{children}</button>
  return (
    <span className="row" style={{ marginTop: 0, display: 'inline-flex' }}>
      <span className="msg">{text}</span>
      <button className="btn danger mini" onClick={() => { setAsking(false); onYes() }}>确定</button>
      <button className="btn ghost mini" onClick={() => setAsking(false)}>取消</button>
    </span>
  )
}
