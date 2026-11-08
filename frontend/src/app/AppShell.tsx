import { Menu, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Outlet, useLocation, useMatches } from 'react-router';

import { JobSelector } from '../features/crawls/JobSelector';
import { ApiStatus } from './ApiStatus';
import { currentHandle } from './navigation';
import { RefreshControls } from './RefreshControls';
import { BrandMark, Sidebar } from './Sidebar';

/** The application frame: the sidebar (a modal drawer on small screens), the header with the
 * page title, the job selector where a page works on one job, the API status and the refresh
 * controls, and the page itself. */
export function AppShell() {
  const handle = currentHandle(useMatches());
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    document.title = `${handle.title} · SiteScope`;
  }, [handle.title]);

  // Close the drawer after any navigation.
  const [lastPath, setLastPath] = useState(location.pathname);
  if (lastPath !== location.pathname) {
    setLastPath(location.pathname);
    setDrawerOpen(false);
  }

  useEffect(() => {
    const drawer = drawerRef.current;
    if (drawer === null) return;
    if (drawerOpen && !drawer.open) drawer.showModal();
    else if (!drawerOpen && drawer.open) drawer.close();
  }, [drawerOpen]);

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-accent-solid px-3 py-2 text-sm text-white focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>

      <aside className="hidden lg:sticky lg:top-0 lg:block lg:h-screen">
        <Sidebar />
      </aside>

      <dialog
        ref={drawerRef}
        aria-label="Navigation"
        onCancel={(event) => {
          event.preventDefault();
          setDrawerOpen(false);
        }}
        className="m-0 h-full max-h-none w-64 max-w-[85vw] border-0 bg-transparent p-0 backdrop:bg-black/60 lg:hidden"
      >
        <div className="relative h-full">
          <Sidebar
            onNavigate={() => {
              setDrawerOpen(false);
            }}
          />
          <button
            type="button"
            onClick={() => {
              setDrawerOpen(false);
            }}
            aria-label="Close navigation"
            className="absolute top-3 right-3 rounded-md p-1.5 text-ink-3 hover:bg-raised hover:text-ink"
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        </div>
      </dialog>

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-30 border-b border-line bg-canvas/90 backdrop-blur">
          <div className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2 sm:px-6 lg:px-8">
            <button
              type="button"
              onClick={() => {
                setDrawerOpen(true);
              }}
              aria-label="Open navigation"
              className="-ml-1 rounded-md p-1.5 text-ink-2 hover:bg-raised hover:text-ink lg:hidden"
            >
              <Menu aria-hidden="true" className="size-5" />
            </button>
            <div className="flex items-center gap-2 lg:hidden">
              <BrandMark />
            </div>
            <h1 className="mr-auto truncate text-base font-semibold tracking-tight text-ink sm:text-lg">
              {handle.title}
            </h1>
            <div className="flex flex-wrap items-center gap-2 sm:gap-3">
              <ApiStatus />
              <RefreshControls />
            </div>
          </div>
          {handle.jobScoped === true && (
            <div className="border-t border-line/70 px-4 py-2 sm:px-6 lg:px-8">
              <JobSelector />
            </div>
          )}
        </header>
        <main id="main" tabIndex={-1} className="flex-1 px-4 py-6 outline-none sm:px-6 lg:px-8">
          <div className="mx-auto max-w-[96rem]">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
