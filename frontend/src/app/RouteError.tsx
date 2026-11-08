import { isRouteErrorResponse, Link, useRouteError } from 'react-router';

import { buttonClasses } from '../components/buttonClasses';
import { EmptyState } from '../components/feedback';

/** The last line of defense: a render error or an unknown route. No stack trace is shown;
 * the browser console has it for developers. */
export function RouteError() {
  const error = useRouteError();
  const notFound = isRouteErrorResponse(error) && error.status === 404;
  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas px-4">
      <EmptyState
        title={notFound ? 'Page not found' : 'This view could not be displayed'}
        action={
          <Link to="/" className={buttonClasses('primary')}>
            Go to the overview
          </Link>
        }
      >
        {notFound
          ? 'The address does not match any page of the dashboard.'
          : 'An unexpected error stopped this page. Reload it, or go back to the overview.'}
      </EmptyState>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <EmptyState
      title="Page not found"
      action={
        <Link to="/" className={buttonClasses('primary')}>
          Go to the overview
        </Link>
      }
    >
      The address does not match any page of the dashboard.
    </EmptyState>
  );
}
