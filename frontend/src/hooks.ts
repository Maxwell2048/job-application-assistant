import { useCallback, useEffect, useRef, useState } from 'react'
import { subscribe, type ServerEvent } from './api'

/** Hash routing: #/board, #/jobs/12, #/submit ... No router dependency needed. */
export function useRoute(): [string[], (path: string) => void] {
  const parse = () => window.location.hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  const [parts, setParts] = useState<string[]>(parse)
  useEffect(() => {
    const onChange = () => setParts(parse())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  const go = useCallback((path: string) => {
    window.location.hash = path.startsWith('#') ? path : `#/${path.replace(/^\//, '')}`
  }, [])
  return [parts, go]
}

/** Load once and again whenever `deps` change or `reload()` is called. */
export function useLoader<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const latest = useRef(0) // only the newest request may write state: a slow old response must not win
  const reload = useCallback(() => {
    const ticket = ++latest.current
    setLoading(true)
    load()
      .then((value) => {
        if (ticket !== latest.current) return
        setData(value)
        setError(null)
      })
      .catch((exc: Error) => { if (ticket === latest.current) setError(exc.message) })
      .finally(() => { if (ticket === latest.current) setLoading(false) })
  }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    reload()
    return () => { latest.current += 1 } // unmounted or deps changed: ignore whatever is still in flight
  }, [reload])
  return { data, error, loading, reload, setData }
}

/** The SSE stream, with a coalescing callback so bursts of events refetch once. */
export function useEvents(onEvent: (event: ServerEvent) => void, debounceMs = 400) {
  const timer = useRef<number | null>(null)
  const latest = useRef(onEvent)
  latest.current = onEvent
  useEffect(() => {
    let pending: ServerEvent | null = null
    const close = subscribe((event) => {
      pending = event
      if (timer.current !== null) window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => {
        if (pending) latest.current(pending)
      }, debounceMs)
    })
    return () => {
      close()
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
  }, [debounceMs])
}

export function useToast(): [string | null, (text: string) => void] {
  const [text, setText] = useState<string | null>(null)
  const timer = useRef<number | null>(null)
  const show = useCallback((value: string) => {
    setText(value)
    if (timer.current !== null) window.clearTimeout(timer.current) // an earlier toast must not clear this one early
    timer.current = window.setTimeout(() => setText(null), 4000)
  }, [])
  return [text, show]
}
