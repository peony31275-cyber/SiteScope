import { Plus } from 'lucide-react';
import { useState, type SubmitEvent } from 'react';
import { useNavigate } from 'react-router';

import { useToast } from '../../app/toastContext';
import { Button } from '../../components/Button';
import { ErrorState, LoadingState, Notice } from '../../components/feedback';
import { describedBy } from '../../components/a11y';
import { Field, Input } from '../../components/Field';
import { ApiError } from '../../lib/api/client';
import { describeError, fieldName } from '../../lib/api/errors';
import type { CrawlPolicy } from '../../lib/api/types';
import { useCrawlPolicy, useCreateCrawl } from './hooks';
import { limitProblem, newIdempotencyKey, seedUrlProblem, suggestedSeeds } from './policy';

type FieldKey = 'seed_url' | 'max_depth' | 'max_pages';
type Errors = Partial<Record<FieldKey, string>>;

interface Values {
  seedUrl: string;
  maxDepth: string;
  maxPages: string;
}

function validate(values: Values, policy: CrawlPolicy): Errors {
  const errors: Errors = {};
  const seed = seedUrlProblem(values.seedUrl, policy);
  if (seed !== null) errors.seed_url = seed;
  const depth = limitProblem(values.maxDepth, {
    min: 0,
    max: policy.max_depth_limit,
    name: 'maximum depth',
  });
  if (depth !== null) errors.max_depth = depth;
  const pages = limitProblem(values.maxPages, {
    min: 1,
    max: policy.max_pages_limit,
    name: 'page limit',
  });
  if (pages !== null) errors.max_pages = pages;
  return errors;
}

/** Server-side field errors (422) on the fields they name; the rest stays a form error. */
function serverErrors(error: unknown): Errors {
  if (!(error instanceof ApiError)) return {};
  const errors: Errors = {};
  for (const item of error.fieldErrors) {
    const field = fieldName(item.loc);
    if (field === 'seed_url' || field === 'max_depth' || field === 'max_pages')
      errors[field] = item.msg;
  }
  return errors;
}

/** The create-job form, limited to this server's crawl policy. */
export function CreateJobForm() {
  const policy = useCrawlPolicy();
  if (policy.isPending) return <LoadingState label="Loading the crawl policy…" />;
  if (policy.isError) {
    return (
      <ErrorState
        error={policy.error}
        onRetry={() => void policy.refetch()}
        retrying={policy.isFetching}
      />
    );
  }
  if (policy.data.approved_hosts.length === 0) {
    return (
      <Notice tone="warning" title="No crawl target is approved on this server">
        Every crawl request would be refused. An operator approves targets with
        SITESCOPE_CRAWL_ALLOWED_HOSTS (docs/development.md, &ldquo;Crawl targets and limits&rdquo;).
      </Notice>
    );
  }
  return <PolicyForm policy={policy.data} />;
}

/** The same request: equal values as they are sent (an edit that changes nothing back and
 * forth, or a click on the target already chosen, is not a new request). */
function sameRequest(a: Values, b: Values): boolean {
  return (
    a.seedUrl.trim() === b.seedUrl.trim() &&
    a.maxDepth.trim() === b.maxDepth.trim() &&
    a.maxPages.trim() === b.maxPages.trim()
  );
}

const FIELD_ORDER: readonly FieldKey[] = ['seed_url', 'max_depth', 'max_pages'];

/** Move focus to the first field with an error, so the error is read out with it. */
function focusFirstInvalid(errors: Errors): void {
  const first = FIELD_ORDER.find((key) => errors[key] !== undefined);
  if (first !== undefined) document.getElementById(`create-${first}`)?.focus();
}

function PolicyForm({ policy }: { policy: CrawlPolicy }) {
  const navigate = useNavigate();
  const { notify } = useToast();
  const create = useCreateCrawl();
  const seeds = suggestedSeeds(policy);
  const wildcards = policy.approved_hosts.filter((host) => host.startsWith('*.'));

  const [values, setValues] = useState<Values>({
    seedUrl: seeds[0] ?? '',
    maxDepth: String(Math.min(2, policy.max_depth_limit)),
    maxPages: String(Math.min(100, policy.max_pages_limit)),
  });
  const [submitted, setSubmitted] = useState(false);
  // One key per intended job: kept while the request stays the same (by value), so retrying a
  // request whose outcome is unknown (a network error, a timeout) cannot create a second job.
  const [attempt, setAttempt] = useState<{ values: Values; key: string } | null>(null);

  const clientErrors = submitted ? validate(values, policy) : {};
  const current = attempt !== null && sameRequest(attempt.values, values);
  const fromServer = current ? serverErrors(create.error) : {};
  const errors: Errors = { ...fromServer, ...clientErrors };
  const failure = create.error;
  const formFailure =
    failure !== null && Object.keys(serverErrors(failure)).length === 0 && current ? failure : null;
  const uncertain =
    formFailure instanceof ApiError &&
    (formFailure.kind === 'network' || formFailure.kind === 'timeout');

  function update(field: keyof Values, value: string) {
    setValues((current) => ({ ...current, [field]: value }));
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitted(true);
    const problems = validate(values, policy);
    if (Object.keys(problems).length > 0) {
      focusFirstInvalid(problems);
      return;
    }
    const key =
      attempt !== null && sameRequest(attempt.values, values) ? attempt.key : newIdempotencyKey();
    setAttempt({ values, key });
    create.mutate(
      {
        body: {
          seed_url: values.seedUrl.trim(),
          max_depth: Number(values.maxDepth),
          max_pages: Number(values.maxPages),
        },
        idempotencyKey: key,
      },
      {
        onSuccess: ({ job, replayed }) => {
          notify(
            replayed
              ? {
                  tone: 'info',
                  title: 'Crawl job already created',
                  message: 'An earlier attempt of this request created it; no second job was made.',
                }
              : {
                  tone: 'success',
                  title: 'Crawl job created',
                  message: 'It is queued until the scheduler starts it.',
                },
          );
          void navigate(`/crawls/${job.id}`);
        },
        onError: (error) => {
          focusFirstInvalid(serverErrors(error));
        },
      },
    );
  }

  const field = (key: FieldKey, hint?: string) => ({
    'aria-invalid': errors[key] !== undefined || undefined,
    'aria-describedby': describedBy(`create-${key}`, hint, errors[key]),
  });

  const seedHint =
    'An http(s) URL on an approved host. The server normalizes it before storing it.';
  const depthHint = `Link hops from the seed: 0 crawls only the seed. At most ${String(policy.max_depth_limit)}.`;
  const pagesHint = `URLs the job may admit, seed included. At most ${String(policy.max_pages_limit)}.`;

  return (
    <form noValidate onSubmit={onSubmit} className="space-y-4" aria-describedby="create-policy">
      <Field id="create-seed_url" label="Seed URL" hint={seedHint} error={errors.seed_url}>
        <Input
          id="create-seed_url"
          name="seed_url"
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          maxLength={4096}
          value={values.seedUrl}
          onChange={(event) => {
            update('seedUrl', event.target.value);
          }}
          className="font-mono"
          {...field('seed_url', seedHint)}
        />
      </Field>
      {seeds.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-xs font-medium text-ink-2">Approved targets</p>
          <ul className="flex flex-wrap gap-1.5">
            {seeds.map((seed) => (
              <li key={seed}>
                <button
                  type="button"
                  onClick={() => {
                    update('seedUrl', seed);
                  }}
                  aria-pressed={values.seedUrl === seed}
                  className="rounded-md border border-line-strong bg-raised px-2 py-1 font-mono text-xs text-ink-2 transition-colors hover:border-accent hover:text-ink aria-pressed:border-accent aria-pressed:text-ink"
                >
                  {seed}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          id="create-max_depth"
          label="Maximum BFS depth"
          hint={depthHint}
          error={errors.max_depth}
        >
          <Input
            id="create-max_depth"
            name="max_depth"
            type="number"
            inputMode="numeric"
            min={0}
            max={policy.max_depth_limit}
            step={1}
            value={values.maxDepth}
            onChange={(event) => {
              update('maxDepth', event.target.value);
            }}
            {...field('max_depth', depthHint)}
          />
        </Field>
        <Field id="create-max_pages" label="Page limit" hint={pagesHint} error={errors.max_pages}>
          <Input
            id="create-max_pages"
            name="max_pages"
            type="number"
            inputMode="numeric"
            min={1}
            max={policy.max_pages_limit}
            step={1}
            value={values.maxPages}
            onChange={(event) => {
              update('maxPages', event.target.value);
            }}
            {...field('max_pages', pagesHint)}
          />
        </Field>
      </div>
      <p id="create-policy" className="text-xs text-ink-3">
        This server approves {policy.approved_hosts.join(', ')}
        {wildcards.length > 0 &&
          ' (a * pattern approves subdomains, not the domain itself)'} over{' '}
        {policy.allowed_schemes.join(' or ')} on port{policy.allowed_ports.length > 1 ? 's' : ''}{' '}
        {policy.allowed_ports.join(', ')}. Every request is checked again by the server.
      </p>
      {formFailure !== null && (
        <ErrorState
          error={formFailure}
          context="action"
          title={uncertain ? 'The request may not have reached the server' : undefined}
          className="text-sm"
        />
      )}
      {uncertain && (
        <p className="text-xs text-ink-3">
          Submitting again is safe: it sends the same idempotency key, so the server creates at most
          one job.
        </p>
      )}
      {formFailure === null && failure !== null && Object.keys(fromServer).length > 0 && (
        <p role="alert" className="text-sm text-critical-ink">
          {describeError(failure).title}: fix the fields marked above.
        </p>
      )}
      <div className="flex justify-end">
        <Button
          type="submit"
          variant="primary"
          busy={create.isPending}
          icon={<Plus aria-hidden="true" className="size-4" />}
        >
          Create crawl job
        </Button>
      </div>
    </form>
  );
}
