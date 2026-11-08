import { ExternalLink } from 'lucide-react';

import { isHttpUrl } from '../lib/urls';
import { cx } from './cx';

interface ExternalUrlProps {
  url: string;
  /** Shown instead of the URL (the URL stays in the tooltip). */
  label?: string;
  className?: string;
}

/**
 * A crawled URL. Untrusted: rendered as text through React's escaping, and a link only if it
 * is http(s), always with rel="noopener noreferrer nofollow" in a new tab (architecture section
 * 12.6). Anything else (javascript:, data:, a malformed value) stays plain text.
 */
export function ExternalUrl({ url, label, className }: ExternalUrlProps) {
  const text = label ?? url;
  if (!isHttpUrl(url)) {
    return (
      <span title={url} className={cx('break-all', className)}>
        {text}
      </span>
    );
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer nofollow"
      title={url}
      className={cx(
        'group inline-flex max-w-full items-baseline gap-1 text-accent-ink hover:underline',
        className,
      )}
    >
      <span className="min-w-0 break-all">{text}</span>
      <ExternalLink
        aria-hidden="true"
        className="size-3 shrink-0 self-center opacity-60 group-hover:opacity-100"
      />
      <span className="sr-only"> (opens in a new tab)</span>
    </a>
  );
}
