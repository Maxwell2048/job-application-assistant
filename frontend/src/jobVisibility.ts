import type { Job } from './api'

export function isArchivedJob(job: Job): boolean {
  return ['screened_out', 'expired', 'blocked'].includes(job.status)
    || (job.screening?.decision === 'skip' && !['queued', 'processing'].includes(job.status))
}

/** Levels the board folds away below the junior board; the user applies mainly for junior roles. */
export const COLLAPSED_LEVELS = ['mid', 'senior'] as const

/** The level the fit screen assigned, if it ran. */
export function fitLevel(job: Job): string | undefined {
  return job.screening?.rule_result.fit?.level
}

/** Mid and senior jobs live in the folded sections; junior, graduate and unclear ones stay on the board. */
export function isCollapsedLevel(job: Job): boolean {
  return (COLLAPSED_LEVELS as readonly string[]).includes(fitLevel(job) ?? '')
}
