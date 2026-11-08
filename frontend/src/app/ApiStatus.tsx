import { useQuery } from '@tanstack/react-query';

import { cx } from '../components/cx';
import { getReadiness } from '../lib/api/endpoints';
import { queryKeys } from '../lib/api/queryKeys';

const CHECK_NAMES: Record<string, string> = { postgres: 'PostgreSQL', redis: 'Redis' };

/** The API's readiness (`/health/ready`: PostgreSQL and Redis), checked every 15 s while the
 * tab is visible. */
export function ApiStatus() {
  const readiness = useQuery({
    queryKey: queryKeys.readiness,
    queryFn: ({ signal }) => getReadiness(signal),
    refetchInterval: 15_000,
    retry: false,
    staleTime: 10_000,
  });

  let tone = 'bg-neutral';
  let label = 'Checking API…';
  let detail = 'Checking whether the API, PostgreSQL and Redis respond.';
  if (readiness.isError) {
    tone = 'bg-critical';
    label = 'API unreachable';
    detail = 'The API did not answer its readiness check.';
  } else if (readiness.data?.status === 'ready') {
    tone = 'bg-good';
    label = 'API online';
    detail = 'The API, PostgreSQL and Redis respond.';
  } else if (readiness.data?.status === 'not_ready') {
    const failing = Object.entries(readiness.data.checks)
      .filter(([, check]) => check.status !== 'ok')
      .map(([name, check]) => `${CHECK_NAMES[name] ?? name} ${check.status}`);
    tone = 'bg-warning';
    label = 'Degraded';
    detail = `The API answers, but ${failing.join(', ') || 'a dependency is unavailable'}.`;
  }

  return (
    <div
      role="status"
      title={detail}
      className="flex items-center gap-2 rounded-full border border-line bg-raised px-2.5 py-1 text-xs font-medium whitespace-nowrap text-ink-2"
    >
      <span aria-hidden="true" className={cx('size-2 rounded-full', tone)} />
      <span>{label}</span>
      <span className="sr-only">: {detail}</span>
    </div>
  );
}
