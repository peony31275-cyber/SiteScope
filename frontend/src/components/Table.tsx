import type { ReactNode, TdHTMLAttributes, ThHTMLAttributes } from 'react';

import { cx } from './cx';

interface TableProps {
  /** Read by screen readers; tables always have one. */
  caption: string;
  children: ReactNode;
  className?: string;
  /** Dim the rows while the next page loads (the previous page stays, no layout jump). */
  dimmed?: boolean;
}

export function Table({ caption, children, className, dimmed = false }: TableProps) {
  return (
    // `relative`: the containing block of the screen-reader-only text inside, so it is clipped
    // with the rows instead of widening the page.
    <div className={cx('relative -mx-4 overflow-x-auto sm:-mx-5', className)}>
      <table
        className={cx(
          'w-full border-collapse text-left text-sm',
          dimmed && 'opacity-60 transition-opacity',
        )}
      >
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export function Th({ className, children, ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cx(
        'border-b border-line bg-surface px-3 py-2 text-xs font-medium whitespace-nowrap text-ink-3 first:pl-4 last:pr-4 sm:first:pl-5 sm:last:pr-5',
        className,
      )}
      {...rest}
    >
      {children}
    </th>
  );
}

export function Td({ className, children, ...rest }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td
      className={cx(
        'border-b border-line/60 px-3 py-2.5 align-top text-ink-2 first:pl-4 last:pr-4 sm:first:pl-5 sm:last:pr-5',
        className,
      )}
      {...rest}
    >
      {children}
    </td>
  );
}

export function Tr({ children, className }: { children: ReactNode; className?: string }) {
  return <tr className={cx('transition-colors hover:bg-raised/60', className)}>{children}</tr>;
}
