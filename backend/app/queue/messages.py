"""Task messages: versioned pointers to rows in PostgreSQL, one contract per stream.

Wire formats, as Redis stream entry fields (all strings):

**Crawl task** (stream ``<prefix>:crawl``, Phase 5):

=========  ==============================================================================
Field      Meaning
=========  ==============================================================================
``v``      the contract version, ``"1"``
``job_id`` the job's UUID
``url_id`` the crawl URL's UUID (``crawl_urls.id``)
=========  ==============================================================================

**Index task** (stream ``<prefix>:index``, Phase 7):

===================  ====================================================================
Field                Meaning
===================  ====================================================================
``v``                the contract version, ``"1"``
``job_id``           the job's UUID
``page_id``          the page's UUID (``pages.id``, also ``search_documents.page_id``)
``content_version``  the page's ``content_version`` when the task was published
===================  ====================================================================

A message carries identifiers only, never page content. Consumers read everything else from
PostgreSQL and act on that state rather than on the message: a duplicate, stale or late message
can never cause more than a refused claim, or an index write that the version guard turns into a
no-op. Other fields are ignored, so later versions may add fields that version 1 readers skip.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Final, Protocol, Self

MESSAGE_VERSION: Final = "1"
# The crawl task's fields (kept for compatibility; see CrawlTask.CONTRACT_FIELDS).
CONTRACT_FIELDS: Final = ("v", "job_id", "url_id")
# Content versions are PostgreSQL INTEGERs: at most 10 decimal digits, below 2**31.
_MAX_CONTENT_VERSION: Final = 2**31 - 1


class MalformedMessageError(ValueError):
    """A stream entry that is not a valid task; it is dead-lettered, never processed."""


class StreamMessage(Protocol):
    """What a task stream carries (``app.queue.streams.TaskStream``)."""

    CONTRACT_FIELDS: ClassVar[tuple[str, ...]]

    def encode(self) -> dict[str, str]: ...

    def identifiers(self) -> dict[str, str]:
        """The decoded identifiers, recorded with a dead letter (never content)."""
        ...


def _check_version(fields: Mapping[str, str]) -> None:
    version = fields.get("v")
    if version != MESSAGE_VERSION:
        raise MalformedMessageError(f"unsupported message version {str(version)[:16]!r}")


@dataclass(frozen=True, slots=True)
class CrawlTask:
    job_id: uuid.UUID
    url_id: uuid.UUID

    CONTRACT_FIELDS: ClassVar[tuple[str, ...]] = CONTRACT_FIELDS

    def encode(self) -> dict[str, str]:
        return {"v": MESSAGE_VERSION, "job_id": str(self.job_id), "url_id": str(self.url_id)}

    def identifiers(self) -> dict[str, str]:
        return {"job_id": str(self.job_id), "url_id": str(self.url_id)}

    @classmethod
    def decode(cls, fields: Mapping[str, str]) -> Self:
        """Validate stream entry fields; raises ``MalformedMessageError`` with the reason."""
        _check_version(fields)
        try:
            return cls(job_id=uuid.UUID(fields["job_id"]), url_id=uuid.UUID(fields["url_id"]))
        except KeyError as exc:
            raise MalformedMessageError(f"missing field {exc.args[0]!r}") from None
        except (ValueError, AttributeError, TypeError):
            raise MalformedMessageError("job_id and url_id must be UUIDs") from None


@dataclass(frozen=True, slots=True)
class IndexTask:
    """Index the page ``page_id`` of job ``job_id`` (Phase 7, ADR-0018).

    ``content_version`` is the page's content version when the task was published. An indexer
    acknowledges a task older than the page's current version without doing anything (the
    current version was published with its own task), and dead-letters one newer than any
    version the page had. It always indexes the page's current content, read under a row lock;
    a version guard and a trigger keep an older index from replacing a newer one.
    """

    job_id: uuid.UUID
    page_id: uuid.UUID
    content_version: int

    CONTRACT_FIELDS: ClassVar[tuple[str, ...]] = ("v", "job_id", "page_id", "content_version")

    def encode(self) -> dict[str, str]:
        return {
            "v": MESSAGE_VERSION,
            "job_id": str(self.job_id),
            "page_id": str(self.page_id),
            "content_version": str(self.content_version),
        }

    def identifiers(self) -> dict[str, str]:
        return {
            "job_id": str(self.job_id),
            "page_id": str(self.page_id),
            "content_version": str(self.content_version),
        }

    @classmethod
    def decode(cls, fields: Mapping[str, str]) -> Self:
        """Validate stream entry fields; raises ``MalformedMessageError`` with the reason."""
        _check_version(fields)
        try:
            job_id, page_id = uuid.UUID(fields["job_id"]), uuid.UUID(fields["page_id"])
            raw_version = fields["content_version"]
        except KeyError as exc:
            raise MalformedMessageError(f"missing field {exc.args[0]!r}") from None
        except (ValueError, AttributeError, TypeError):
            raise MalformedMessageError("job_id and page_id must be UUIDs") from None
        if not (
            isinstance(raw_version, str)
            and raw_version.isascii()
            and raw_version.isdigit()
            and len(raw_version) <= 10
            and 1 <= int(raw_version) <= _MAX_CONTENT_VERSION
        ):
            raise MalformedMessageError("content_version must be a positive integer")
        return cls(job_id=job_id, page_id=page_id, content_version=int(raw_version))
