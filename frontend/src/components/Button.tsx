import type { ComponentProps, ReactNode } from 'react';
import { Link, type LinkProps } from 'react-router';

import { buttonClasses, type ButtonSize, type ButtonVariant } from './buttonClasses';
import { cx } from './cx';
import { Spinner } from './Spinner';

interface ButtonProps extends ComponentProps<'button'> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner and disables the button while an action runs. */
  busy?: boolean;
  icon?: ReactNode;
}

export function Button({
  variant,
  size,
  busy = false,
  icon,
  className,
  children,
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cx(buttonClasses(variant, size), className)}
      disabled={disabled === true || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <Spinner size={14} /> : icon}
      {children}
    </button>
  );
}

interface ButtonLinkProps extends LinkProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
}

/** A router link that looks like a button (navigation, not an action). */
export function ButtonLink({ variant, size, icon, className, children, ...rest }: ButtonLinkProps) {
  return (
    <Link className={cx(buttonClasses(variant, size), className)} {...rest}>
      {icon}
      {children}
    </Link>
  );
}
