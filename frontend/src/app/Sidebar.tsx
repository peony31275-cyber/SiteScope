import { NavLink, useLocation, useSearchParams } from 'react-router';

import { cx } from '../components/cx';
import { recallJob } from '../hooks/useSelectedJob';
import { isUuid } from '../lib/urls';
import { NAV_ITEMS } from './navigation';

export function BrandMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 32 32" className="size-7 shrink-0">
      <rect width="32" height="32" rx="7" fill="#1d2025" />
      <circle cx="14" cy="14" r="7.5" fill="none" stroke="#3987e5" strokeWidth="2.5" />
      <circle cx="14" cy="14" r="2.5" fill="#3987e5" />
      <path d="M19.5 19.5 25 25" stroke="#eceef1" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

export function Sidebar({
  onNavigate,
  className,
}: {
  onNavigate?: () => void;
  className?: string;
}) {
  const [params] = useSearchParams();
  const { pathname } = useLocation();
  // The job of this view: `?job=`, or the job whose detail page this is, or the last one viewed.
  const fromQuery = params.get('job');
  const fromDetail = /^\/crawls\/([^/]+)$/.exec(pathname)?.[1] ?? null;
  const job =
    fromQuery !== null && isUuid(fromQuery)
      ? fromQuery
      : fromDetail !== null && isUuid(fromDetail)
        ? fromDetail
        : recallJob();

  return (
    <div className={cx('flex h-full flex-col border-r border-line bg-surface', className)}>
      <div className="flex h-14 items-center gap-2.5 border-b border-line px-5">
        <BrandMark />
        <div className="leading-tight">
          <p className="text-sm font-semibold tracking-tight text-ink">SiteScope</p>
          <p className="text-[11px] text-ink-3">Crawl &amp; search infrastructure</p>
        </div>
      </div>
      <nav aria-label="Main" className="flex-1 overflow-y-auto px-3 py-4">
        <ul className="space-y-0.5">
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.jobScoped && job !== null ? `${item.to}?job=${job}` : item.to}
                end={item.to === '/'}
                onClick={onNavigate}
                className={({ isActive }) =>
                  cx(
                    'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                    isActive ? 'bg-overlay text-ink' : 'text-ink-2 hover:bg-raised hover:text-ink',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <item.icon
                      aria-hidden="true"
                      className={cx('size-4', isActive ? 'text-accent-ink' : 'text-ink-3')}
                    />
                    {item.label}
                  </>
                )}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className="border-t border-line px-5 py-3 text-[11px] leading-relaxed text-ink-3">
        Data from the SiteScope API. Crawled content is shown as text, never as HTML.
      </div>
    </div>
  );
}
