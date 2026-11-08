/**
 * Bar charts (Recharts), drawn to the dataviz mark specs: bars at most 20px thick with a 4px
 * rounded data end and a square baseline, hairline grid, text in the text inks, a tooltip on
 * hover and keyboard focus (Recharts' accessibility layer), and a table twin for every chart.
 */
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from 'recharts';

import { formatCompact, formatNumber } from '../../lib/format';
import { CHART, SERIES } from './chartTheme';
import { ChartTable } from './ChartTable';

const TICK = { fill: CHART.tick, fontSize: CHART.fontSize };

/** Tooltip: the category, then each value (strong) with its series name and a line key. */
function ChartTooltip({ active, payload, label }: TooltipContentProps) {
  if (!active || payload.length === 0) return null;
  return (
    <div className="rounded-md border border-line-strong bg-raised px-3 py-2 text-xs shadow-xl">
      <p className="mb-1 text-ink-3">{label}</p>
      {payload.map((entry) => (
        <p key={String(entry.name)} className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="h-0.5 w-3 rounded"
            style={{ backgroundColor: entry.color }}
          />
          <span className="font-semibold text-ink tabular-nums">
            {formatNumber(Number(entry.value))}
          </span>
          <span className="text-ink-2">{entry.name}</span>
        </p>
      ))}
    </div>
  );
}

export interface BarDatum {
  label: string;
  value: number;
}

interface HorizontalBarChartProps {
  data: readonly BarDatum[];
  /** What the bars count, such as "Jobs" (the tooltip and table column). */
  valueName: string;
  /** The category column of the table, such as "Status". */
  categoryName: string;
  /** The chart's accessible name. */
  caption: string;
}

/** One series of categories, compared by length; the value is printed at each bar's tip. */
export function HorizontalBarChart({
  data,
  valueName,
  categoryName,
  caption,
}: HorizontalBarChartProps) {
  const rows = data.map((datum) => ({ label: datum.label, [valueName]: datum.value }));
  const height = Math.max(120, data.length * 34 + 24);
  return (
    <figure aria-label={caption}>
      <BarChart
        data={rows}
        layout="vertical"
        responsive
        style={{ width: '100%', height }}
        margin={{ top: 4, right: 48, bottom: 4, left: 4 }}
        barCategoryGap={8}
        title={caption}
      >
        <CartesianGrid horizontal={false} stroke={CHART.grid} />
        <XAxis
          type="number"
          allowDecimals={false}
          tick={TICK}
          tickFormatter={formatCompact}
          stroke={CHART.axis}
        />
        <YAxis
          type="category"
          dataKey="label"
          width={136}
          tick={TICK}
          stroke={CHART.axis}
          tickLine={false}
        />
        <Tooltip content={ChartTooltip} cursor={{ fill: CHART.cursor }} />
        <Bar
          dataKey={valueName}
          fill={SERIES[0]}
          maxBarSize={20}
          radius={[0, 4, 4, 0]}
          isAnimationActive={false}
        >
          <LabelList
            dataKey={valueName}
            position="right"
            fill={CHART.tick}
            fontSize={CHART.fontSize}
            formatter={(value) => (typeof value === 'number' ? formatNumber(value) : value)}
          />
        </Bar>
      </BarChart>
      <ChartTable
        caption={caption}
        category={categoryName}
        series={[valueName]}
        rows={data.map((datum) => ({ label: datum.label, values: [datum.value] }))}
      />
    </figure>
  );
}

interface GroupedColumnChartProps {
  categories: readonly string[];
  /** At most three series (the validated categorical slots). */
  series: readonly { name: string; values: readonly number[] }[];
  categoryName: string;
  caption: string;
}

/** Columns per category, one per series (at most three), with a legend above the plot. */
export function GroupedColumnChart({
  categories,
  series,
  categoryName,
  caption,
}: GroupedColumnChartProps) {
  const rows = categories.map((label, index) => ({
    label,
    ...Object.fromEntries(series.map((item) => [item.name, item.values[index] ?? 0])),
  }));
  return (
    <figure aria-label={caption}>
      <ul className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
        {series.map((item, index) => (
          <li key={item.name} className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="size-2.5 rounded-sm"
              style={{ backgroundColor: SERIES[index] }}
            />
            {item.name}
          </li>
        ))}
      </ul>
      <BarChart
        data={rows}
        responsive
        style={{ width: '100%', height: 240 }}
        margin={{ top: 8, right: 8, bottom: 4, left: 0 }}
        barGap={2}
        barCategoryGap="20%"
        title={caption}
      >
        <CartesianGrid vertical={false} stroke={CHART.grid} />
        <XAxis dataKey="label" tick={TICK} stroke={CHART.axis} tickLine={false} />
        <YAxis
          allowDecimals={false}
          tick={TICK}
          tickFormatter={formatCompact}
          stroke={CHART.axis}
          width={44}
        />
        <Tooltip content={ChartTooltip} cursor={{ fill: CHART.cursor }} />
        {series.map((item, index) => (
          <Bar
            key={item.name}
            dataKey={item.name}
            fill={SERIES[index]}
            maxBarSize={18}
            radius={[4, 4, 0, 0]}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
      <ChartTable
        caption={caption}
        category={categoryName}
        series={series.map((item) => item.name)}
        rows={categories.map((label, index) => ({
          label,
          values: series.map((item) => item.values[index] ?? 0),
        }))}
      />
    </figure>
  );
}
