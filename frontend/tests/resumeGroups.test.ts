import assert from 'node:assert/strict'
import { test } from 'node:test'
import { excludeArchivedResumes, groupResumes } from '../src/resumeGroups.ts'
import type { RunSummary } from '../src/api.ts'

const run = (id: string, date: string, status: 'submitted' | 'unsubmitted' = 'unsubmitted'): RunSummary => ({
  run_id: id, generation_date: date, application_status: status, has_resume: true,
  run_status: 'complete', title: 'Junior Developer', company: 'Example',
  counts: {}, docs: [], downloads: [], can_interview: false,
})

test('newest dates and versions first, with every version kept', () => {
  const rows = [run('20261012-010000-aaaaaa', '2026-10-12'), run('20261013-010000-bbbbbb', '2026-10-13'), run('20261013-020000-cccccc', '2026-10-13', 'submitted')]
  const groups = groupResumes(rows)
  assert.deepEqual(groups.map((g) => g.date), ['2026-10-13', '2026-10-12'])
  assert.equal(groups[0].items[0].run_id, '20261013-020000-cccccc')
  assert.equal(groups.flatMap((g) => g.items).length, 3)
})

test('status filter and text search agree, unfinished runs excluded', () => {
  const rows = [run('a', '2026-10-13'), run('b', '2026-10-13', 'submitted'), { ...run('c', '2026-10-13'), has_resume: false }]
  assert.equal(groupResumes(rows, 'submitted', 'EXAMPLE')[0].items.length, 1)
  assert.equal(groupResumes(rows, 'unsubmitted')[0].items.length, 1)
  assert.deepEqual(groupResumes(rows, 'all', 'missing'), [])
})



test('skipping a job excludes all its resume versions from active counts and groups', () => {
  const rows = [{ ...run('old', '2026-10-12'), job_id: 61 }, { ...run('new', '2026-10-13'), job_id: 61 },
    { ...run('submitted', '2026-10-13', 'submitted'), job_id: 61 }, { ...run('active', '2026-10-13'), job_id: 57 }, run('legacy', '2026-09-12')]
  const visible = excludeArchivedResumes(rows, new Set([61]))
  assert.deepEqual(visible.map((r) => r.run_id), ['active', 'legacy'])
  assert.equal(groupResumes(visible, 'unsubmitted').flatMap((g) => g.items).length, 2)
  assert.equal(rows[2].application_status, 'submitted') // stored history is untouched
  assert.equal(rows.length, 5)
  assert.equal(excludeArchivedResumes(rows, new Set()).length, 5) // becomes visible again if restored
})


test('server archive flag excludes a resume even when its job is outside the loaded page', () => {
  const rows = [{ ...run('skipped', '2026-10-13'), job_id: 61, archived: true }, run('active', '2026-10-13')]
  assert.deepEqual(excludeArchivedResumes(rows, new Set()).map((r) => r.run_id), ['active'])
})
