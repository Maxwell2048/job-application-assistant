import assert from 'node:assert/strict'
import { test } from 'node:test'
import { coalesce } from '../src/coalesce.ts'

const tick = () => new Promise((resolve) => setTimeout(resolve, 5))

test('calls during a refresh cause exactly one follow-up refresh', async () => {
  let calls = 0
  let active = 0
  let maxActive = 0
  const refresh = coalesce(async () => {
    calls += 1
    active += 1
    maxActive = Math.max(maxActive, active)
    await tick()
    active -= 1
  })
  const first = refresh()
  refresh()
  refresh()
  refresh()
  await first
  assert.equal(calls, 2)
  assert.equal(maxActive, 1)
})

test('a quiet refresh runs once, and a later call starts a new one', async () => {
  let calls = 0
  const refresh = coalesce(async () => { calls += 1; await tick() })
  await refresh()
  assert.equal(calls, 1)
  await refresh()
  assert.equal(calls, 2)
})

test('a failing refresh does not block later ones', async () => {
  let calls = 0
  const refresh = coalesce(async () => {
    calls += 1
    if (calls === 1) throw new Error('offline')
  })
  await refresh()
  await refresh()
  assert.equal(calls, 2)
})
