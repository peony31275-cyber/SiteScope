/**
 * Test environment (Vitest + jsdom). jsdom lacks some browser APIs the dashboard uses; these
 * are test doubles with the smallest behavior the components rely on.
 */
import '@testing-library/jest-dom/vitest';

import { cleanup, configure } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

// Pages load lazily (their own chunks): under a full parallel run, the first render of a page
// can take longer than Testing Library's default one second.
configure({ asyncUtilTimeout: 3000 });

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.useRealTimers();
});

// <dialog>: jsdom has the element but not its modal methods.
if (typeof HTMLDialogElement.prototype.showModal !== 'function') {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
}

// ResizeObserver (charts, the graph canvas): never reports a size in jsdom. Assigned, not
// stubbed: `unstubGlobals` restores stubbed globals after every test.
class ResizeObserverDouble {
  observe = (): undefined => undefined;
  unobserve = (): undefined => undefined;
  disconnect = (): undefined => undefined;
}
globalThis.ResizeObserver = ResizeObserverDouble;

// Pointer capture (graph panning).
if (typeof Element.prototype.setPointerCapture !== 'function') {
  Element.prototype.setPointerCapture = () => undefined;
  Element.prototype.releasePointerCapture = () => undefined;
}
