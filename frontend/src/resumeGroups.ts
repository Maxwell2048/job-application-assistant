import type { RunSummary } from './api'

export type ApplicationFilter = 'all' | 'unsubmitted' | 'submitted'

export function groupResumes(runs: RunSummary[], filter: ApplicationFilter = 'all', query = '') {
  const groups = new Map<string, RunSummary[]>()
  const q = query.trim().toLowerCase()
  for (const run of runs) {
    if (!run.has_resume) continue
    const status = run.application_status ?? 'unsubmitted'
    if (filter !== 'all' && status !== filter) continue
    if (q && !`${run.title ?? ''} ${run.company ?? ''} ${run.jd_file ?? ''}`.toLowerCase().includes(q)) continue
    const date = run.generation_date ?? '日期未知'
    groups.set(date, [...(groups.get(date) ?? []), run])
  }
  return [...groups.entries()].sort(([a], [b]) => a === '日期未知' ? 1 : b === '日期未知' ? -1 : b.localeCompare(a))
    .map(([date, items]) => ({ date, items: items.sort((a, b) => b.run_id.localeCompare(a.run_id)) }))
}


/** A skipped job removes every version from the active application list. */
export function excludeArchivedResumes(runs: RunSummary[], archivedJobIds: ReadonlySet<number>) {
  return runs.filter((run) => run.archived !== true && (run.job_id == null || !archivedJobIds.has(run.job_id)))
}


/** Submitted jobs no longer leave unused versions in the active pending list.
 * Actual version submission records are never inferred or overwritten.
 */
export function partitionApplicationResumes(runs: RunSummary[]) {
  const submittedJobs = new Set(runs.filter((r) => r.job_id != null &&
    (r.application_status === 'submitted' || r.job_application_status === 'submitted'))
    .map((r) => r.job_id!))
  const active: RunSummary[] = []
  const history: RunSummary[] = []
  for (const run of runs) {
    if (!run.has_resume) continue
    if (run.application_status !== 'submitted' && run.job_id != null && submittedJobs.has(run.job_id)) {
      history.push(run)
    } else {
      active.push(run)
    }
  }
  return { active, history }
}
