// Typed client for the backend JSON API. Every shape here mirrors a Pydantic
// model in backend/app/api/schemas.py or a dict the service layer returns.

export type Stage = { key: string; label: string; status: string; seconds: number | null; message: string }

export type RunSummary = {
  archived?: boolean
  job_status?: string | null
  run_id: string
  has_resume?: boolean
  generation_date?: string | null
  application_status?: "submitted" | "unsubmitted"
  submitted_at?: string | null
  job_id?: number | null
  title?: string
  company?: string
  run_status: string | null
  started_at?: string | null
  jd_file?: string
  counts: Record<string, number>
  /** Evidence coverage of the job's conditions (not responsibilities, not preferred extras); {} without matches. */
  coverage?: Coverage
  docs: { stem: string; label: string; file: string }[]
  downloads: string[]
  can_interview: boolean
  missing?: boolean
}

export type Task = {
  id: string
  kind: string
  jd_name: string
  job_id: number | null
  created_at: string
  status: string
  error: string | null
  run_id: string | null
  stages: Stage[]
  run?: RunSummary | null
}

export type Coverage = {
  total?: number
  direct?: number
  related?: number
  insufficient?: number
  error?: number
  hard_gaps?: number
}

/** The fit gate's result (backend services/screen.py fit_gate). */
export type Fit = {
  processing_status?: "ok" | "error"
  decision: string
  skips: string[]
  flags: string[]
  level: string
  level_quote?: string
  min_years?: number
  years_mandatory?: boolean
  years_quote?: string
  /** false when the years quote has no number: the figure is the model's reading, not the JD's */
  years_quoted?: boolean
  role_family: string
  required_stack?: string[]
  missing_stack?: string[]
  degree?: string
  degree_quote?: string
  constraints?: { kind: string; quote: string }[]
  reason: string
  known_stack_size: number
}

export type Screening = {
  model_verdict: string
  quote: string
  decision: string
  rule_result: { exclude?: boolean; reasons?: string[]; flags?: string[]; fit?: Fit | null; work_rights?: { processing_status?: "ok" | "error"; reason?: string } | null }
  created_at: string
}

export type Job = {
  id: number
  source: string
  external_id: string
  title: string
  company: string
  url: string
  location: string
  work_type: string
  listed_at: string
  first_seen: string
  last_seen: string
  status: string
  repost_of: number | null
  note: string
  jd_path: string
  screening: Screening | null
  runs: RunSummary[]
}

export type JobDetail = Job & { jd_text: string; tasks: Partial<Task>[] }

/** started: screening now; busy: a round is running, the next one screens it; repost: same JD as another job */
export type ScreeningStarted = { job_id: number; status: string; screening: 'started' | 'busy' | 'repost' }

export type Health = {
  online: boolean
  model: string | null
  /** paused: why the queue is waiting (the model is offline), or '' */
  queue: { current: Task | null; waiting: string[]; paused?: string }
  autopilot_enabled: boolean
}

/** focused: graduate/junior/entry level/trainee keywords; broad: every ICT job in Perth */
export type SearchMode = 'focused' | 'broad'

export type Brief = { job_id: number; title: string; company: string; url: string; reason: string; quote: string }

export type TickReport = {
  id: string
  trigger: string
  mode?: SearchMode
  /** false: a search-only round; eligible jobs went to "ready" instead of the queue */
  generate?: boolean
  ready?: Brief[]
  started_at: string
  finished_at?: string
  seconds?: number
  model_online: boolean | null
  sources: Record<string, Record<string, unknown>>
  collected: number
  new: number
  updated: number
  seen: number
  repost: number
  no_jd: number
  queued: Brief[]
  flagged: Brief[]
  screened_out: Brief[]
  held: Brief[]
  notes: string[]
  errors: string[]
}

export type AutopilotStatus = {
  enabled: boolean
  running: boolean
  interval_hours: number
  next_run_at: string | null
  last_report: TickReport | null
  collector_configured: boolean
  model_launcher_configured: boolean
  mode: SearchMode
  /** true: screened jobs are queued for generation; false: they wait in the "ready" column */
  auto_generate: boolean
  mode_limits: Record<SearchMode, { max_pages: number; max_details: number }>
}

export type Settings = {
  resume_path: string
  outputs_dir: string
  jobs_dir: string
  database: string
  autopilot_enabled: boolean
  autopilot_interval_hours: number
  collector_dir: string | null
  model_launcher: string | null
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...init })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = await response.json()
      detail = body.detail ?? body.error ?? detail
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<Health>('/api/health'),
  startModel: () => request<{ started: boolean; online: boolean }>('/api/model/start', { method: 'POST' }),
  settings: () => request<Settings>('/api/settings'),
  jobs: async (status?: string) => {
    const jobs: Job[] = []
    for (let offset = 0; ; offset += 1000) {
      const batch = await request<Job[]>(`/api/jobs?limit=1000&offset=${offset}${status ? `&status=${encodeURIComponent(status)}` : ''}`)
      jobs.push(...batch)
      if (batch.length < 1000) return jobs
    }
  },
  job: (id: number) => request<JobDetail>(`/api/jobs/${id}`),
  submit: (jd_text: string, jd_name: string, with_interview: boolean) =>
    request<Task>('/api/jobs', { method: 'POST', body: JSON.stringify({ jd_text, jd_name, with_interview }) }),
  /** Paste a JD to be screened like a collected one instead of generating straight away. */
  submitForScreening: (jd_text: string, jd_name: string) =>
    request<ScreeningStarted>('/api/jobs', { method: 'POST', body: JSON.stringify({ jd_text, jd_name, screen_first: true }) }),
  /** The JD of a job known only by its title, pasted by hand; screened right away. */
  supplementJd: (id: number, jd_text: string) =>
    request<ScreeningStarted>(`/api/jobs/${id}/jd`, { method: 'POST', body: JSON.stringify({ jd_text }) }),
  decide: (id: number, action: 'run' | 'skip' | 'rerun', with_interview = false) =>
    request<Task | Job>(`/api/jobs/${id}/decision`, { method: 'POST', body: JSON.stringify({ action, with_interview }) }),
  interview: (id: number) => request<Task>(`/api/jobs/${id}/interview`, { method: 'POST' }),
  tasks: () => request<Task[]>('/api/tasks'),
  task: (id: string) => request<Task>(`/api/tasks/${id}`),
  cancel: (id: string) => request<Task>(`/api/tasks/${id}/cancel`, { method: 'POST' }),
  runs: () => request<RunSummary[]>('/api/runs'),
  resumes: () => request<RunSummary[]>('/api/runs?resumes_only=true&limit=5000'),
  setApplication: (runId: string, submitted: boolean) =>
    request<RunSummary>(`/api/runs/${runId}/application`, { method: 'PUT', body: JSON.stringify({ submitted }) }),
  doc: (runId: string, name: string) => request<{ html: string; markdown: string }>(`/api/runs/${runId}/docs/${name}`),
  downloadUrl: (runId: string, file: string) => `/api/runs/${runId}/download/${encodeURIComponent(file)}`,
  autopilot: () => request<AutopilotStatus>('/api/autopilot/status'),
  setMode: (mode: SearchMode) =>
    request<{ mode: SearchMode }>('/api/autopilot/mode', { method: 'POST', body: JSON.stringify({ mode }) }),
  setAutoGenerate: (enabled: boolean) =>
    request<{ auto_generate: boolean }>('/api/autopilot/generate', { method: 'POST', body: JSON.stringify({ enabled }) }),
  generateReady: () => request<{ queued: number }>('/api/jobs/generate-ready', { method: 'POST' }),
  tick: (body: { max_pages?: number; max_details?: number; sources?: string[]; mode?: SearchMode; generate?: boolean }) =>
    request<{ started: boolean }>('/api/autopilot/tick', { method: 'POST', body: JSON.stringify(body) }),
  reports: () => request<TickReport[]>('/api/autopilot/reports'),
}

export type ServerEvent = { type: string; data: unknown }

/** Subscribe to the server-sent event stream; returns a function that closes it. */
export function subscribe(onEvent: (event: ServerEvent) => void): () => void {
  const source = new EventSource('/api/events')
  const handler = (type: string) => (message: MessageEvent) => {
    try {
      onEvent({ type, data: JSON.parse(message.data) })
    } catch {
      onEvent({ type, data: message.data })
    }
  }
  for (const type of ['task', 'queue', 'autopilot', 'jobs']) source.addEventListener(type, handler(type))
  return () => source.close()
}
