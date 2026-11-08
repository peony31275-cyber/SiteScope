import { Search } from 'lucide-react';
import { useState, type SubmitEvent } from 'react';
import { useSearchParams } from 'react-router';

import { describedBy } from '../../components/a11y';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { EmptyState, ErrorState, LoadingState, Notice } from '../../components/feedback';
import { Field, Input, Select } from '../../components/Field';
import { PageNav } from '../../components/Pager';
import { ExternalUrl } from '../../components/ExternalUrl';
import { useNow } from '../../hooks/useNow';
import type { SearchResponse } from '../../lib/api/types';
import { formatDateTime, formatNumber, formatRelative, pathOf } from '../../lib/format';
import { JobGate } from '../crawls/JobGate';
import { HighlightedText } from './HighlightedText';
import { useSearch } from './hooks';
import {
  lastServablePage,
  parsePage,
  parsePageSize,
  SEARCH_MAX_QUERY_LENGTH,
  SEARCH_PAGE_SIZES,
  searchPageCount,
} from './paging';

const SYNTAX_HINT =
  'Words must all match; use "quotes" for a phrase, or between alternatives, and -word to exclude. Words match by their English stem.';

export function SearchPage() {
  // Keyed by job: another job's results are never kept on screen while this one's load.
  return <JobGate>{(jobId) => <SearchView key={jobId} jobId={jobId} />}</JobGate>;
}

function SearchView({ jobId }: { jobId: string }) {
  const [params, setParams] = useSearchParams();
  const q = params.get('q') ?? '';
  const page = parsePage(params.get('page'));
  const pageSize = parsePageSize(params.get('size'));
  const search = useSearch({ jobId, q, page, pageSize });

  // The input follows the address (back and forward), and is free to edit in between.
  const [draft, setDraft] = useState(q);
  const [shownQuery, setShownQuery] = useState(q);
  if (shownQuery !== q) {
    setShownQuery(q);
    setDraft(q);
  }
  const [draftSize, setDraftSize] = useState(pageSize);
  const [inputError, setInputError] = useState<string | null>(null);

  function go(next: { q?: string; page?: number; size?: number }) {
    setParams((current) => {
      const updated = new URLSearchParams(current);
      if (next.q !== undefined) updated.set('q', next.q);
      updated.set('page', String(next.page ?? page));
      updated.set('size', String(next.size ?? pageSize));
      return updated;
    });
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (draft.trim() === '') {
      setInputError('Enter the words to search for.');
      document.getElementById('search-q')?.focus();
      return;
    }
    setInputError(null);
    go({ q: draft, page: 1, size: draftSize });
  }

  return (
    <div className="space-y-6">
      <Card
        title="Search the indexed pages"
        description="PostgreSQL full-text search of one crawl job, ranked by relevance."
      >
        <form
          role="search"
          noValidate
          onSubmit={onSubmit}
          className="flex flex-col gap-3 md:flex-row md:items-start"
        >
          <Field
            id="search-q"
            label="Search query"
            hint={SYNTAX_HINT}
            error={inputError}
            className="flex-1"
          >
            <Input
              id="search-q"
              type="search"
              value={draft}
              maxLength={SEARCH_MAX_QUERY_LENGTH}
              placeholder='consumer groups, "exact phrase", -excluded'
              onChange={(event) => {
                setDraft(event.target.value);
              }}
              aria-invalid={inputError !== null || undefined}
              aria-describedby={describedBy('search-q', SYNTAX_HINT, inputError)}
            />
          </Field>
          <Field id="search-size" label="Results per page" className="md:w-36">
            <Select
              id="search-size"
              value={draftSize}
              onChange={(event) => {
                setDraftSize(Number(event.target.value));
              }}
            >
              {SEARCH_PAGE_SIZES.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </Select>
          </Field>
          <Button
            type="submit"
            variant="primary"
            className="md:mt-[1.375rem]"
            icon={<Search aria-hidden="true" className="size-4" />}
          >
            Search
          </Button>
        </form>
      </Card>

      {q.trim() === '' ? (
        <EmptyState icon={<Search className="size-5" />} title="Search this job's pages">
          Only indexed pages are searched: pages not indexed yet, noindex pages and other jobs never
          appear.
        </EmptyState>
      ) : search.isPending ? (
        <LoadingState label="Searching…" />
      ) : search.isError ? (
        <ErrorState
          error={search.error}
          onRetry={() => void search.refetch()}
          retrying={search.isFetching}
        />
      ) : (
        <SearchResults
          response={search.data}
          page={page}
          pageSize={pageSize}
          loading={search.isFetching && search.isPlaceholderData}
          onPage={(next) => {
            go({ page: next });
          }}
        />
      )}
    </div>
  );
}

interface SearchResultsProps {
  response: SearchResponse;
  page: number;
  pageSize: number;
  loading: boolean;
  onPage: (page: number) => void;
}

function SearchResults({ response, page, pageSize, loading, onPage }: SearchResultsProps) {
  const now = useNow();
  const { indexing } = response;
  const first = (page - 1) * pageSize + 1;
  const pageCount = searchPageCount(response.total, pageSize);
  const servable = (lastServablePage(pageSize) - 1) * pageSize + pageSize;

  return (
    <section aria-label="Search results" className="space-y-4">
      {(indexing.status === 'pending' || indexing.status === 'running') && (
        <Notice tone="info" title="Indexing is still in progress">
          {formatNumber(indexing.documents_by_status.indexed)} of{' '}
          {formatNumber(indexing.documents_total)} pages are indexed. Pages not indexed yet are not
          searched: a later search may find more.
        </Notice>
      )}
      {!response.searchable ? (
        <Notice tone="warning" title="Nothing to search for">
          After removing stop words (such as &ldquo;the&rdquo;) and excluded words (-word), the
          query &ldquo;{response.query}&rdquo; has no term the index can look up. Try other words.
        </Notice>
      ) : (
        <p className="text-sm text-ink-2" aria-live="polite">
          {response.total === 0 ? (
            <>No indexed page matches &ldquo;{response.query}&rdquo;.</>
          ) : (
            <>
              <span className="font-medium text-ink">{formatNumber(response.total)}</span> result
              {response.total === 1 ? '' : 's'} for &ldquo;{response.query}&rdquo;
              {response.results.length > 0 && (
                <>
                  {' '}
                  · showing {formatNumber(first)}–
                  {formatNumber(first + response.results.length - 1)}
                </>
              )}
              . Scores are PostgreSQL ts_rank values: they order this search only.
            </>
          )}
        </p>
      )}
      {response.results.length > 0 && (
        <ol className={loading ? 'space-y-3 opacity-60 transition-opacity' : 'space-y-3'}>
          {response.results.map((result, index) => (
            <li key={result.page_id} className="rounded-xl border border-line bg-surface p-4">
              <div className="flex items-start justify-between gap-3">
                <h3 className="min-w-0 text-sm font-semibold">
                  <span className="mr-2 text-xs font-normal text-ink-3 tabular-nums">
                    #{first + index}
                  </span>
                  <ExternalUrl url={result.url} label={result.title ?? pathOf(result.url)} />
                </h3>
                <span
                  className="shrink-0 text-xs text-ink-3 tabular-nums"
                  title="PostgreSQL ts_rank of the page for this query"
                >
                  score {result.score.toFixed(4)}
                </span>
              </div>
              <p className="mt-0.5 truncate font-mono text-xs text-ink-3" title={result.url}>
                {result.url}
              </p>
              <p className="mt-2 text-sm leading-relaxed text-ink-2">
                <HighlightedText text={result.snippet} ranges={result.highlights} />
              </p>
              <p className="mt-2 text-xs text-ink-3">
                <span title={formatDateTime(result.crawled_at)}>
                  Crawled {formatRelative(result.crawled_at, now)}
                </span>{' '}
                ·{' '}
                <span title={formatDateTime(result.indexed_at)}>
                  indexed {formatRelative(result.indexed_at, now)}
                </span>
              </p>
            </li>
          ))}
        </ol>
      )}
      {response.searchable && response.total > 0 && response.results.length === 0 && (
        <p className="text-sm text-ink-3">This page is past the last result.</p>
      )}
      {response.total > servable && (
        <p className="text-xs text-ink-3">
          The server serves the first {formatNumber(servable)} results of a search; refine the query
          to reach the others.
        </p>
      )}
      <PageNav
        label="Search result pages"
        page={page}
        pageCount={pageCount}
        onPage={onPage}
        loading={loading}
      />
    </section>
  );
}
