/** Accessibility wiring shared by components. */

/** The `aria-describedby` of a field's control: its error if any, else its hint. */
export function describedBy(
  id: string,
  hint: unknown,
  error: string | null | undefined,
): string | undefined {
  if (error) return `${id}-error`;
  return hint === undefined ? undefined : `${id}-hint`;
}

/** The props of a tab panel controlled by `Tabs` with the same `idPrefix`. */
export function tabPanelProps(idPrefix: string, key: string) {
  return {
    role: 'tabpanel' as const,
    id: `${idPrefix}-panel-${key}`,
    'aria-labelledby': `${idPrefix}-tab-${key}`,
    tabIndex: 0,
  };
}

/** 1 … 4 5 [6] 7 8 … 20: the first and last pages, and two around the current one (null is a
 * gap). */
export function pageNumbers(page: number, pageCount: number): (number | null)[] {
  const shown = new Set([1, pageCount, page - 2, page - 1, page, page + 1, page + 2]);
  const pages = [...shown].filter((item) => item >= 1 && item <= pageCount).sort((a, b) => a - b);
  const result: (number | null)[] = [];
  for (const [index, item] of pages.entries()) {
    const previous = pages[index - 1];
    if (previous !== undefined && item - previous > 1) result.push(null);
    result.push(item);
  }
  return result;
}
