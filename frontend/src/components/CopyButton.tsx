import { Check, Copy } from 'lucide-react';
import { useEffect, useState } from 'react';

import { cx } from './cx';

/** Copies a value (a job ID) to the clipboard and says so, for screen readers too. */
export function CopyButton({
  value,
  label,
  className,
}: {
  value: string;
  label: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return undefined;
    const timer = setTimeout(() => {
      setCopied(false);
    }, 2000);
    return () => {
      clearTimeout(timer);
    };
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      // No clipboard access (insecure context, or denied): the value stays selectable.
    }
  }

  return (
    <button
      type="button"
      onClick={() => {
        void copy();
      }}
      aria-label={copied ? `${label}: copied` : label}
      title={label}
      className={cx(
        'inline-flex size-6 items-center justify-center rounded text-ink-3 hover:bg-raised hover:text-ink',
        className,
      )}
    >
      {copied ? (
        <Check aria-hidden="true" className="size-3.5 text-good" />
      ) : (
        <Copy aria-hidden="true" className="size-3.5" />
      )}
    </button>
  );
}
