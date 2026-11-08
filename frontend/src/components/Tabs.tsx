import { useRef, type KeyboardEvent, type ReactNode } from 'react';

import { cx } from './cx';

export interface TabItem<Key extends string> {
  key: Key;
  label: ReactNode;
}

interface TabsProps<Key extends string> {
  items: readonly TabItem<Key>[];
  selected: Key;
  onSelect: (key: Key) => void;
  label: string;
  idPrefix: string;
}

/** WAI-ARIA tabs: one tab stop, arrow keys move between tabs, Home and End jump. The panel is
 * rendered by the caller with `tabPanelProps`. */
export function Tabs<Key extends string>({
  items,
  selected,
  onSelect,
  label,
  idPrefix,
}: TabsProps<Key>) {
  const refs = useRef(new Map<Key, HTMLButtonElement>());

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const index = items.findIndex((item) => item.key === selected);
    const last = items.length - 1;
    const target = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: last }[event.key];
    if (target === undefined) return;
    event.preventDefault();
    const next = items[(target + items.length) % items.length];
    if (next === undefined) return;
    onSelect(next.key);
    refs.current.get(next.key)?.focus();
  }

  return (
    <div
      role="tablist"
      aria-label={label}
      className="flex gap-1 overflow-x-auto border-b border-line"
    >
      {items.map((item) => {
        const active = item.key === selected;
        return (
          <button
            key={item.key}
            ref={(element) => {
              if (element) refs.current.set(item.key, element);
              else refs.current.delete(item.key);
            }}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${item.key}`}
            aria-selected={active}
            aria-controls={active ? `${idPrefix}-panel-${item.key}` : undefined}
            tabIndex={active ? 0 : -1}
            onKeyDown={onKeyDown}
            onClick={() => {
              onSelect(item.key);
            }}
            className={cx(
              '-mb-px flex items-center gap-2 border-b-2 px-3 py-2.5 text-sm font-medium whitespace-nowrap transition-colors',
              active ? 'border-accent text-ink' : 'border-transparent text-ink-3 hover:text-ink-2',
            )}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
