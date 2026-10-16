import type { Job } from './api'

export function isArchivedJob(job: Job): boolean {
  return ['screened_out', 'expired', 'blocked'].includes(job.status)
    || (job.screening?.decision === 'skip' && !['queued', 'processing'].includes(job.status))
}
