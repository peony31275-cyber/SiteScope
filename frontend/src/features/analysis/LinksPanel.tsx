import { Link2, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { useSearchParams } from 'react-router';

import { Badge } from '../../components/Badge';
import { ExternalUrl } from '../../components/ExternalUrl';
import { EmptyState, ErrorState, LoadingState } from '../../components/feedback';
import { Select } from '../../components/Field';
import { CursorPager } from '../../components/Pager';
import { Table, Td, Th, Tr } from '../../components/Table';
import { useCursorPager } from '../../hooks/useCursorPager';
import type { LinkItem, LinksQuery } from '../../lib/api/types';
import { formatDateTime, pathOf, shortId } from '../../lib/format';
import {
  ANALYSIS_PAGE_SIZE,
  parseLinkClass,
  parseSourcePage,
  parseTriState,
  triStateValue,
  type TriState,
} from './filters';
import { useCrawlLinks } from './hooks';
import { LINK_CLASS_ORDER, LINK_CLASSES } from './linkClasses';

/** Every stored link with its classification, filterable as the API allows. */
export function LinksPanel({ jobId }: { jobId: string }) {
  const [params, setParams] = useSearchParams();
  const linkClass = parseLinkClass(params.get('class'));
  const verified = parseTriState(params.get('verified'));
  const internal = parseTriState(params.get('internal'));
  const redirected = parseTriState(params.get('redirected'));
  const source = parseSourcePage(params.get('source'));
  const pager = useCursorPager(
    JSON.stringify([jobId, linkClass, verified, internal, redirected, source]),
  );
  const query: LinksQuery = {
    limit: ANALYSIS_PAGE_SIZE,
    cursor: pager.cursor,
    classification: linkClass,
    verified: triStateValue(verified),
    internal: triStateValue(internal),
    redirected: triStateValue(redirected),
    source_page_id: source,
  };
  const links = useCrawlLinks(jobId, query);

  function setFilter(name: string, value: string | null) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (value === null || value === '' || value === 'any') next.delete(name);
      else next.set(name, value);
      return next;
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <FilterSelect
          id="links-class"
          label="Classification"
          value={linkClass ?? ''}
          onChange={(value) => {
            setFilter('class', value);
          }}
        >
          <option value="">Any classification</option>
          {LINK_CLASS_ORDER.map((name) => (
            <option key={name} value={name}>
              {LINK_CLASSES[name].label} ({LINK_CLASSES[name].verified ? 'verified' : 'unverified'})
            </option>
          ))}
        </FilterSelect>
        <TriStateSelect
          id="links-verified"
          label="Verification"
          value={verified}
          options={['Verified', 'Unverified']}
          onChange={(value) => {
            setFilter('verified', value);
          }}
        />
        <TriStateSelect
          id="links-internal"
          label="Scope"
          value={internal}
          options={['Internal', 'External']}
          onChange={(value) => {
            setFilter('internal', value);
          }}
        />
        <TriStateSelect
          id="links-redirected"
          label="Target redirected"
          value={redirected}
          options={['Redirected (3xx)', 'Not redirected']}
          onChange={(value) => {
            setFilter('redirected', value);
          }}
        />
        {source !== null && (
          <button
            type="button"
            onClick={() => {
              setFilter('source', null);
            }}
            className="inline-flex h-9 items-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-3 text-xs text-ink hover:bg-accent/20"
          >
            On page <span className="font-mono">{shortId(source)}</span>
            <X aria-hidden="true" className="size-3.5" />
            <span className="sr-only">(remove this filter)</span>
          </button>
        )}
      </div>

      {links.isPending ? (
        <LoadingState label="Loading links…" />
      ) : links.isError ? (
        <ErrorState
          error={links.error}
          onRetry={() => void links.refetch()}
          retrying={links.isFetching}
        />
      ) : links.data.items.length === 0 ? (
        <EmptyState
          icon={<Link2 className="size-5" />}
          title="No stored link matches these filters"
        />
      ) : (
        <div>
          <Table caption="Stored links with their classification" dimmed={links.isPlaceholderData}>
            <thead>
              <tr>
                <Th>Source page</Th>
                <Th>Anchor text</Th>
                <Th>Target</Th>
                <Th>Classification</Th>
                <Th>Target response</Th>
                <Th>Checked</Th>
              </tr>
            </thead>
            <tbody>
              {links.data.items.map((link) => (
                <LinkRow key={`${link.source_page_id}|${link.target_url}`} link={link} />
              ))}
            </tbody>
          </Table>
          <CursorPager
            label="Link list pages"
            page={pager.page}
            hasPrevious={pager.hasPrevious}
            hasNext={links.data.next_cursor !== null}
            onPrevious={pager.previous}
            onNext={() => {
              if (links.data.next_cursor !== null) pager.next(links.data.next_cursor);
            }}
            loading={links.isFetching && links.isPlaceholderData}
          />
        </div>
      )}
    </div>
  );
}

function LinkRow({ link }: { link: LinkItem }) {
  const meaning = LINK_CLASSES[link.classification];
  return (
    <Tr>
      <Td className="max-w-56">
        <span className="block truncate font-mono text-xs" title={link.source_url}>
          {pathOf(link.source_url)}
        </span>
      </Td>
      <Td className="max-w-48">
        <span className="block truncate" title={link.anchor_text ?? undefined}>
          {link.anchor_text ?? <span className="text-ink-3">(none)</span>}
        </span>
      </Td>
      <Td className="max-w-72">
        <ExternalUrl
          url={link.target_url}
          label={link.internal ? pathOf(link.target_url) : link.target_url}
          className="font-mono text-xs"
        />
        <span className="mt-0.5 block text-xs text-ink-3">
          {link.internal ? 'internal' : 'external'}
          {link.nofollow && ' · nofollow'}
        </span>
      </Td>
      <Td>
        <span title={meaning.description}>
          <Badge tone={meaning.tone}>{meaning.label}</Badge>
        </span>
        <span className="mt-1 block text-xs text-ink-3">
          {meaning.verified ? 'verified' : 'unverified'}
        </span>
      </Td>
      <Td className="text-xs">
        <TargetResponse link={link} />
      </Td>
      <Td className="text-xs whitespace-nowrap">{formatDateTime(link.checked_at)}</Td>
    </Tr>
  );
}

const NOT_REQUESTED: Partial<Record<NonNullable<LinkItem['target_state']>, string>> = {
  pending: 'not crawled yet',
  queued: 'not crawled yet',
  in_progress: 'being crawled',
  skipped: 'not requested (skipped)',
  cancelled: 'not requested (cancelled)',
  failed: 'no response',
};

function TargetResponse({ link }: { link: LinkItem }) {
  if (link.target_http_status === null) {
    const state = link.target_state;
    return (
      <span className="text-ink-3">
        {state === null ? 'never admitted' : (NOT_REQUESTED[state] ?? state)}
      </span>
    );
  }
  if (link.final_url === null) {
    return <span className="text-ink tabular-nums">{link.target_http_status}</span>;
  }
  return (
    <span>
      <span className="text-ink tabular-nums">{link.target_http_status}</span>
      <span className="text-ink-3">
        {' '}
        → {link.redirect_hops} hop{link.redirect_hops === 1 ? '' : 's'} →{' '}
      </span>
      <span className="font-mono" title={link.final_url}>
        {pathOf(link.final_url)}
      </span>{' '}
      <span className="text-ink tabular-nums">{link.final_http_status ?? 'no response'}</span>
    </span>
  );
}

function FilterSelect({
  id,
  label,
  value,
  onChange,
  children,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-xs font-medium text-ink-2">
        {label}
      </label>
      <Select
        id={id}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
        className="w-auto min-w-36"
      >
        {children}
      </Select>
    </div>
  );
}

function TriStateSelect({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id: string;
  label: string;
  value: TriState;
  options: readonly [string, string];
  onChange: (value: string) => void;
}) {
  return (
    <FilterSelect id={id} label={label} value={value} onChange={onChange}>
      <option value="any">Any</option>
      <option value="true">{options[0]}</option>
      <option value="false">{options[1]}</option>
    </FilterSelect>
  );
}
