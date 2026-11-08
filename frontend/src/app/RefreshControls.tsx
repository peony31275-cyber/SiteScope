import { useIsFetching, useQueryClient } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';

import { Button } from '../components/Button';
import { cx } from '../components/cx';
import { Select } from '../components/Field';
import { POLL_CHOICES, useSettings } from './settingsContext';

/** Refresh the page's data now, and choose how often running jobs refresh by themselves. */
export function RefreshControls() {
  const client = useQueryClient();
  const fetching = useIsFetching() > 0;
  const { pollIntervalMs, setPollIntervalMs } = useSettings();

  return (
    <div className="flex items-center gap-2">
      <label htmlFor="auto-refresh" className="sr-only">
        Auto-refresh running jobs
      </label>
      <Select
        id="auto-refresh"
        value={pollIntervalMs === null ? 'off' : String(pollIntervalMs)}
        onChange={(event) => {
          setPollIntervalMs(event.target.value === 'off' ? null : Number(event.target.value));
        }}
        className="h-8 w-auto text-xs"
        title="How often running jobs refresh by themselves"
      >
        {pollIntervalMs !== null &&
          !(POLL_CHOICES as readonly number[]).includes(pollIntervalMs) && (
            <option value={pollIntervalMs}>Auto-refresh {pollIntervalMs / 1000} s</option>
          )}
        {POLL_CHOICES.map((value) => (
          <option key={value} value={value}>
            Auto-refresh {value / 1000} s
          </option>
        ))}
        <option value="off">Auto-refresh off</option>
      </Select>
      <Button
        size="sm"
        onClick={() => {
          void client.invalidateQueries();
        }}
        aria-label="Refresh data now"
        title="Refresh data now"
        icon={
          <RefreshCw aria-hidden="true" className={cx('size-3.5', fetching && 'animate-spin')} />
        }
      >
        <span className="hidden md:inline">Refresh</span>
      </Button>
    </div>
  );
}
