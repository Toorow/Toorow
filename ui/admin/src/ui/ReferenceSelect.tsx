/**
 * ReferenceSelect — a searchable, VALIDATED selector over a governed vocabulary.
 *
 * Story 48.3 AC1 requires the reporting currency to use "a searchable validated
 * ISO 4217 selector" and the reporting timezone "a searchable validated IANA
 * identifier selector; neither is a free-text or hard-coded subset". Project
 * Settings shipped two `Input`s: the only feedback on a typo arrived much later,
 * and nothing on the screen told an operator which values were even legal.
 *
 * It is not `Select` and it is not `ChoiceGroup`, and the distinction is the
 * length of the list rather than a preference:
 *
 *   Select           one of a handful, all options shipped in the markup.
 *   ChoiceGroup      one of a few, every option readable without a click.
 *   ReferenceSelect  one of hundreds, fetched and ranked by the server. 156
 *                    tender currencies and 313 selectable zones cannot be a
 *                    hard-coded subset without becoming a second authority.
 *
 * Two properties it must have, and both are the reason it exists:
 *
 * **The value is always a code from the vocabulary.** Typing narrows the list;
 * it never becomes the value. `onChange` fires only from a chosen item, so an
 * unresolvable string cannot reach a Change Set.
 *
 * **It is keyboard operable.** Arrow keys move the active option, Enter commits,
 * Escape closes and restores, and the listbox/option roles are real so a screen
 * reader announces the position. AC11 asks for this explicitly.
 *
 * The server ranks; this component does not re-sort. Two places ordering the
 * same list by two rules is how "the same query gives a different answer" starts.
 */
import { ChevronDownIcon } from "lucide-react"
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react"
import { Input } from "../components/ui/input"
import { Popover, PopoverAnchor, PopoverContent } from "../components/ui/popover"

export type ReferenceItem = {
  code: string
  display_name: string
  /** Rendered as a muted trailing hint when present (minor unit, area). */
  hint?: string | null
}

export type ReferenceSelectProps = {
  /** The endpoint that ranks and returns items. Must accept `q` and `limit`. */
  endpoint: string
  value: string | null
  onChange: (code: string) => void
  /** Human label of the vocabulary, used in the empty and error states. */
  vocabularyLabel: string
  placeholder?: string
  disabled?: boolean
  id?: string
  "aria-describedby"?: string
  /** Injected in tests so the component is provable without a network. */
  fetchItems?: (query: string) => Promise<ReferenceItem[]>
}

const LIMIT = 25

export function ReferenceSelect({
  endpoint,
  value,
  onChange,
  vocabularyLabel,
  placeholder,
  disabled = false,
  id,
  fetchItems,
  ...rest
}: ReferenceSelectProps) {
  const generatedId = useId()
  const inputId = id ?? generatedId
  const listId = `${inputId}-listbox`

  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [items, setItems] = useState<ReferenceItem[]>([])
  /**
   * WHICH query the items in state answer.
   *
   * Loading is debounced, so between a keystroke and its response `items` still
   * holds the PREVIOUS answer. Committing from it means a person types `XXX`,
   * presses Enter, and leaves with `EUR` — a value from the vocabulary, so the
   * guard above is satisfied, and not remotely what they asked for. Found by
   * `CreateOrgCurrency.test.tsx` under full-suite load on 2026-08-04: the same
   * assertion passes in isolation, because there the response wins the race.
   */
  const [loadedQuery, setLoadedQuery] = useState<string | null>(null)
  const [active, setActive] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const inputRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(
    async (text: string) => {
      setLoading(true)
      setError(null)
      try {
        if (fetchItems) {
          setItems(await fetchItems(text))
          setLoadedQuery(text)
          return
        }
        const { apiFetch } = await import("../lib/apiFetch")
        const url = `${endpoint}?q=${encodeURIComponent(text)}&limit=${LIMIT}`
        const response = await apiFetch(url, { credentials: "same-origin" })
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        const body = (await response.json()) as { items?: ReferenceItem[] }
        setItems(body.items ?? [])
        setLoadedQuery(text)
      } catch {
        // An unreadable vocabulary must not silently render as "no matches":
        // that reads as "this value does not exist", which is a different claim.
        setError(`The ${vocabularyLabel} list is unavailable right now.`)
        setItems([])
        setLoadedQuery(text)
      } finally {
        setLoading(false)
      }
    },
    [endpoint, fetchItems, vocabularyLabel],
  )

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const timer = setTimeout(() => {
      if (!cancelled) void load(query)
    }, 120)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [open, query, load])

  useEffect(() => {
    setActive(0)
  }, [items])

  const commit = (item: ReferenceItem) => {
    onChange(item.code)
    setQuery("")
    setOpen(false)
    inputRef.current?.focus()
  }

  /** The list on screen does not answer what is typed right now. */
  const stale = loadedQuery !== query

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (disabled) return
    if (!open && (event.key === "ArrowDown" || event.key === "Enter")) {
      setOpen(true)
      event.preventDefault()
      return
    }
    if (!open) return
    if (event.key === "ArrowDown") {
      setActive((index) => Math.min(index + 1, Math.max(items.length - 1, 0)))
      event.preventDefault()
    } else if (event.key === "ArrowUp") {
      setActive((index) => Math.max(index - 1, 0))
      event.preventDefault()
    } else if (event.key === "Enter") {
      // Enter on an empty list does nothing. It must NEVER commit the typed
      // text: that is exactly the free-text field this component replaces. And
      // never from a list that answers an older query -- see `loadedQuery`.
      const item = stale ? undefined : items[active]
      if (item) commit(item)
      event.preventDefault()
    } else if (event.key === "Escape") {
      setQuery("")
      setOpen(false)
      event.preventDefault()
    }
  }

  const display = useMemo(() => (open ? query : (value ?? "")), [open, query, value])

  return (
    <Popover open={open} onOpenChange={setOpen}>
      {/* The chevron is not decoration. Without it this control is an `<input>`
          with a placeholder, pixel-identical to the free-text field above it,
          and nothing on the screen says a list exists -- so a person reads
          "type your currency", types nothing, and leaves the value unset
          believing they were never offered a choice. The affordance is what
          makes it a selector. It is `aria-hidden` and click-through: the input
          keeps the combobox role, and clicking the chevron falls through to the
          input's own `onClick`, which opens the list. */}
      <PopoverAnchor asChild>
        <div className="relative">
          <Input
            {...rest}
            id={inputId}
            ref={inputRef}
            role="combobox"
            aria-expanded={open}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={
              open && !stale && items[active] ? `${listId}-${items[active].code}` : undefined
            }
            autoComplete="off"
            disabled={disabled}
            value={display}
            className="pr-10"
            placeholder={placeholder ?? `Search ${vocabularyLabel}`}
            onFocus={() => setOpen(true)}
            // `onFocus` alone is not enough: committing a choice returns focus to
            // the input, so the next click on it fires no focus event and the list
            // never reopens. Clicking a combobox has to reopen it.
            onClick={() => setOpen(true)}
            onChange={(event) => {
              setQuery(event.target.value)
              setOpen(true)
            }}
            onKeyDown={onKeyDown}
          />
          <ChevronDownIcon
            aria-hidden="true"
            data-slot="reference-select-indicator"
            className={
              "pointer-events-none absolute top-1/2 right-3.5 size-4 -translate-y-1/2 text-text-secondary transition-transform " +
              (open ? "rotate-180" : "")
            }
          />
        </div>
      </PopoverAnchor>
      <PopoverContent
        align="start"
        className="w-[var(--radix-popover-trigger-width)] min-w-72 p-0"
        onOpenAutoFocus={(event) => event.preventDefault()}
      >
        <ul id={listId} role="listbox" aria-label={vocabularyLabel} className="max-h-72 overflow-y-auto py-1">
          {error ? (
            <li role="alert" className="px-3 py-2 text-ui text-danger">
              {error}
            </li>
          ) : stale || (loading && items.length === 0) ? (
            // A stale list is not shown at all, not even greyed: an option that
            // answers a previous query is one click away from being committed.
            <li role="status" className="px-3 py-2 text-ui text-text-secondary">
              Searching…
            </li>
          ) : items.length === 0 ? (
            <li className="px-3 py-2 text-ui text-text-secondary">
              No {vocabularyLabel} matches “{query}”.
            </li>
          ) : (
            items.map((item, index) => (
              <li
                key={item.code}
                id={`${listId}-${item.code}`}
                role="option"
                aria-selected={item.code === value}
                data-active={index === active ? "true" : undefined}
                className={
                  "flex cursor-pointer items-baseline justify-between gap-3 px-3 py-2 text-ui " +
                  (index === active ? "bg-surface-muted text-text" : "text-text")
                }
                onMouseEnter={() => setActive(index)}
                onMouseDown={(event) => {
                  // mousedown, not click: the input blurs first otherwise and the
                  // popover closes before the choice is registered.
                  event.preventDefault()
                  commit(item)
                }}
              >
                <span className="font-mono text-caption">{item.code}</span>
                <span className="flex-1 truncate">{item.display_name}</span>
                {item.hint ? (
                  <span className="text-caption text-text-secondary">{item.hint}</span>
                ) : null}
              </li>
            ))
          )}
        </ul>
      </PopoverContent>
    </Popover>
  )
}
