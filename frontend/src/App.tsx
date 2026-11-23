import { useCallback, useEffect, useState } from 'react'
import { api, type Health, type Job, type Task } from './api'
import { Badge, STATUS_LABEL, Stepper } from './components'
import { useEvents, useRoute, useToast } from './hooks'
import { ResumePage } from './ResumePage'
import { isArchivedJob } from './jobVisibility'
import { ArchivePage, AutopilotPage, Board, JobPage, RunsPage, SettingsPage, SubmitPage } from './pages'

const NAV = [
  ['board', '岗位看板'], ['resumes', '简历与投递'], ['archive', '归档岗位'], ['submit', '添加岗位 JD'], ['autopilot', '岗位采集'], ['runs', '生成记录'], ['settings', '系统设置'],
] as const

export default function App() {
  const [route, go] = useRoute()
  const page = route[0] || 'board'
  const [health, setHealth] = useState<Health | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [version, setVersion] = useState(0)
  const [toast, showToast] = useToast()

  const refresh = useCallback(() => {
    api.health().then(setHealth).catch(() => setHealth(null))
    api.jobs().then(setJobs).catch(() => undefined)
    api.tasks().then(setTasks).catch(() => undefined)
    setVersion((v) => v + 1)
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => {
    const timer = window.setInterval(() => api.health().then(setHealth).catch(() => setHealth(null)), 15000)
    return () => window.clearInterval(timer)
  }, [])
  useEvents((event) => {
    if (event.type === 'task' && (event.data as Task)?.status === 'running') {
      api.tasks().then(setTasks).catch(() => undefined)
      return
    }
    refresh()
  })

  const boardJobs = jobs.filter((j) => !isArchivedJob(j))
  const archivedJobs = jobs.filter(isArchivedJob)
  const live = tasks.filter((t) => t.status === 'running' || t.status === 'queued')
  const counts = (statuses: string[]) => jobs.filter((j) => statuses.includes(j.status)).length

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">Job Assistant<span>求职申请工作台</span></div>
        <nav className="nav">
          {NAV.map(([key, label]) => (
            <a key={key} href={`#/${key}`} className={page === key ? 'active' : ''}>
              {label}
              {key === 'archive' && <span className="count">{archivedJobs.length}</span>}
              {key === 'board' && <span className="count">{counts(['new', 'held', 'queued', 'processing'])} 待处理</span>}
            </a>
          ))}
        </nav>
        <div className="status">
          <div>
            <span className={`dot ${health?.online ? 'on' : ''}`} />模型 {health?.online ? `在线 · ${health.model}` : '离线'}
            {health && !health.online && (
              <button className="btn ghost mini" style={{ marginLeft: 8 }}
                onClick={() => api.startModel().then((r) => showToast(r.started ? '已启动模型，约一两分钟后就绪' : '模型已在线')).catch((e: Error) => showToast(e.message))}>
                启动
              </button>
            )}
          </div>
          <div>队列：{health?.queue.current ? '1 个在生成' : '空闲'}{health && health.queue.waiting.length > 0 ? `，${health.queue.waiting.length} 个等待` : ''}</div>
          {health?.queue.paused && <div className="msg bad">⏸ {health.queue.paused}</div>}
          {(health?.restart_needed?.length ?? 0) > 0 && (
            <div className="msg bad" title={health?.restart_needed?.join('\n')}>
              ⟳ 后端代码已更新（{health?.restart_needed?.length} 个文件），重启后端（start.bat）后生效；筛选和采集仍按旧规则运行
            </div>
          )}
          <div>定时采集：{health?.autopilot_enabled ? '定时开启' : '手动'}</div>
          <div>看板：{boardJobs.length} 个岗位，{boardJobs.filter((j) => j.status === 'done').length} 份已生成</div>
        </div>
      </aside>
      <main className="main">
        {live.length > 0 && page !== 'jobs' && (
          <div className="card" style={{ marginBottom: 18 }}>
            {live.map((t) => (
              <div key={t.id}>
                <h2 style={{ marginBottom: 4 }}>
                  正在生成 <Badge kind={t.status}>{STATUS_LABEL[t.status]}</Badge> {t.jd_name}
                  {t.job_id && <> · <a href={`#/jobs/${t.job_id}`}>详情</a></>}
                </h2>
                <Stepper task={t} />
              </div>
            ))}
          </div>
        )}
        {page === 'board' && <Board jobs={boardJobs} archivedJobs={archivedJobs} onOpen={(id) => go(`jobs/${id}`)} version={version} onToast={showToast} onChanged={refresh} />}
        {page === 'jobs' && route[1] && <JobPage id={Number(route[1])} tasks={tasks} onToast={showToast} refresh={version} onChanged={refresh} />}
        {page === 'submit' && (
          <SubmitPage online={!!health?.online} onToast={showToast}
            onSubmitted={(task) => { showToast('已加入队列'); refresh(); if (task.job_id) go(`jobs/${task.job_id}`) }}
            onScreening={(jobId, message) => { showToast(message); refresh(); go(`jobs/${jobId}`) }} />
        )}
        {page === 'autopilot' && <AutopilotPage onToast={showToast} />}
        {page === 'archive' && <ArchivePage jobs={archivedJobs} onOpen={(id) => go(`jobs/${id}`)} />}
        {page === 'resumes' && <ResumePage jobs={jobs} version={version} onToast={showToast} onChanged={refresh} />}
        {page === 'runs' && <RunsPage />}
        {page === 'settings' && <SettingsPage />}
      </main>
      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
