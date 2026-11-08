import type { HighlightRange } from '../../lib/api/types';
import { highlightSegments } from './highlight';

/** A snippet with its matched words marked. Every part is a React text node: the snippet is
 * crawled content and is never parsed as HTML. The segments of a snippet never reorder, so
 * their position is a stable key. */
export function HighlightedText({
  text,
  ranges,
}: {
  text: string;
  ranges: readonly HighlightRange[];
}) {
  return (
    <>
      {highlightSegments(text, ranges).map((segment, index) =>
        segment.highlighted ? (
          <mark key={index} className="hit">
            {segment.text}
          </mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </>
  );
}
