import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { ConfirmDialog } from './ConfirmDialog';
import { ExternalUrl } from './ExternalUrl';
import { Meter, SegmentedBar } from './Meter';

function DialogHarness({ onConfirm, busy = false }: { onConfirm: () => void; busy?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
        }}
      >
        Open
      </button>
      <ConfirmDialog
        open={open}
        title="Delete everything?"
        confirmLabel="Delete"
        busy={busy}
        onConfirm={onConfirm}
        onCancel={() => {
          setOpen(false);
        }}
      >
        <p>This cannot be undone.</p>
      </ConfirmDialog>
    </>
  );
}

describe('ConfirmDialog', () => {
  it('focuses the safe choice first and closes on Escape without confirming', async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(<DialogHarness onConfirm={onConfirm} />);
    await user.click(screen.getByRole('button', { name: 'Open' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete everything?' });
    expect(dialog).toHaveAccessibleDescription('This cannot be undone.');
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();

    dialog.dispatchEvent(new Event('cancel', { cancelable: true })); // what Escape fires
    await vi.waitFor(() => {
      expect(dialog).not.toHaveAttribute('open');
    });
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('confirms, and waits while the action runs', async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<DialogHarness onConfirm={onConfirm} />);
    await user.click(screen.getByRole('button', { name: 'Open' }));
    await user.click(screen.getByRole('button', { name: 'Delete' }));
    expect(onConfirm).toHaveBeenCalledOnce();

    rerender(<DialogHarness onConfirm={onConfirm} busy />);
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Delete' })).toHaveAttribute('aria-busy', 'true');
  });
});

describe('ExternalUrl', () => {
  it('links an http(s) URL safely, in a new tab', () => {
    render(<ExternalUrl url="https://docs.example.com/a" />);
    const link = screen.getByRole('link', { name: /docs\.example\.com\/a/ });
    expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAccessibleName(/^https:\/\/docs\.example\.com\/a ?\(opens in a new tab\)$/);
  });

  it('keeps any other scheme as text', () => {
    render(<ExternalUrl url="javascript:alert(1)" label="Click" />);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText('Click')).toHaveAttribute('title', 'javascript:alert(1)');
  });
});

describe('Meter and SegmentedBar', () => {
  it('expose their numbers to assistive technology', () => {
    render(
      <>
        <Meter label="URLs finished" value={7} max={12} />
        <SegmentedBar
          label="Documents"
          segments={[
            { key: 'a', label: 'Indexed', value: 3, colorClassName: 'bg-good' },
            { key: 'b', label: 'Failed', value: 0, colorClassName: 'bg-critical' },
          ]}
        />
      </>,
    );
    expect(screen.getByRole('progressbar', { name: 'URLs finished' })).toHaveAttribute(
      'aria-valuetext',
      '7 of 12 (58%)',
    );
    expect(screen.getByRole('img', { name: 'Documents: 3 Indexed, 0 Failed' })).toBeInTheDocument();
    expect(screen.getByText('Failed')).toHaveTextContent('Failed00%');
  });
});
