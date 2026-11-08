import { useEffect, useId, useRef, type ReactNode } from 'react';

import { Button } from './Button';
import type { ButtonVariant } from './buttonClasses';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  confirmVariant?: ButtonVariant;
  /** The action is running: both buttons wait, and Escape does not close the dialog. */
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * A modal confirmation on the native <dialog>: the browser makes the rest of the page inert,
 * traps focus, and returns focus when it closes. The safe choice (cancel) has focus first, so
 * Enter or Space never confirms by accident; Escape cancels.
 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  cancelLabel = 'Cancel',
  confirmVariant = 'primary',
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const bodyId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) return;
    if (open && !dialog.open) {
      dialog.showModal();
      cancelRef.current?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      aria-describedby={bodyId}
      onCancel={(event) => {
        event.preventDefault(); // Escape: the parent decides, through `open`.
        if (!busy) onCancel();
      }}
      className="m-auto w-[min(28rem,calc(100vw-2rem))] rounded-xl border border-line-strong bg-surface p-0 text-ink shadow-2xl backdrop:bg-black/60"
    >
      <div className="p-5">
        <h2 id={titleId} className="text-base font-semibold text-ink">
          {title}
        </h2>
        <div id={bodyId} className="mt-2 space-y-2 text-sm text-ink-2">
          {children}
        </div>
      </div>
      <div className="flex justify-end gap-2 border-t border-line bg-raised/40 px-5 py-3">
        <Button ref={cancelRef} onClick={onCancel} disabled={busy}>
          {cancelLabel}
        </Button>
        <Button variant={confirmVariant} onClick={onConfirm} busy={busy}>
          {confirmLabel}
        </Button>
      </div>
    </dialog>
  );
}
