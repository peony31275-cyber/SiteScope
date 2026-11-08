import { formatNumber } from '../../lib/format';

interface ChartTableProps {
  caption: string;
  /** The first column's header (the category). */
  category: string;
  /** One header per value column. */
  series: readonly string[];
  rows: readonly { label: string; values: readonly number[] }[];
}

/** The table twin of a chart: every plotted value, readable without the chart or a pointer. */
export function ChartTable({ caption, category, series, rows }: ChartTableProps) {
  return (
    <details className="mt-3 text-xs">
      <summary className="cursor-pointer text-ink-3 hover:text-ink-2">
        Show the data as a table
      </summary>
      <table className="mt-2 w-full text-left">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-line text-ink-3">
            <th scope="col" className="py-1.5 pr-3 font-medium">
              {category}
            </th>
            {series.map((name) => (
              <th key={name} scope="col" className="py-1.5 pr-3 text-right font-medium">
                {name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label} className="border-b border-line/50 text-ink-2">
              <th scope="row" className="py-1.5 pr-3 font-normal">
                {row.label}
              </th>
              {row.values.map((value, index) => (
                <td
                  key={series[index] ?? index}
                  className="py-1.5 pr-3 text-right text-ink tabular-nums"
                >
                  {formatNumber(value)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
