import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isCollapsedLevel } from '../src/jobVisibility.ts'
import type { Job } from '../src/api.ts'

const job = (level?: string): Job => ({
  id: 1, status: 'ready', title: 'Developer', company: 'Example', runs: [],
  screening: level === undefined ? null : { rule_result: { fit: { level, role_family: 'software', flags: [] } } },
} as unknown as Job)

test('mid and senior jobs are folded away from the board', () => {
  assert.equal(isCollapsedLevel(job('mid')), true)
  assert.equal(isCollapsedLevel(job('senior')), true)
})

test('junior, graduate, unclear and unscreened jobs stay on the board', () => {
  for (const level of ['junior', 'graduate', 'unclear', undefined]) {
    assert.equal(isCollapsedLevel(job(level)), false, String(level))
  }
})
