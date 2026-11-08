import { keepPreviousData, useQuery } from '@tanstack/react-query';

import { searchPages } from '../../lib/api/endpoints';
import { queryKeys } from '../../lib/api/queryKeys';

interface SearchParams {
  jobId: string | null;
  q: string;
  page: number;
  pageSize: number;
}

/** One page of results. Not polled: a search is run when asked for. The previous page stays
 * on screen (dimmed) while the next one loads. */
export function useSearch({ jobId, q, page, pageSize }: SearchParams) {
  const query = { job_id: jobId ?? '', q, page, page_size: pageSize };
  return useQuery({
    queryKey: queryKeys.search(query),
    queryFn: ({ signal }) => searchPages(query, signal),
    enabled: jobId !== null && q.trim() !== '',
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}
