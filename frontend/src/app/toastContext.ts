import { createContext, useContext } from 'react';

export type ToastTone = 'success' | 'info' | 'warning' | 'error';

export interface ToastInput {
  tone: ToastTone;
  title: string;
  message?: string;
}

export interface ToastValue {
  notify: (toast: ToastInput) => void;
}

export const ToastContext = createContext<ToastValue | null>(null);

export function useToast(): ToastValue {
  const value = useContext(ToastContext);
  if (value === null) throw new Error('useToast must be used inside ToastProvider');
  return value;
}
