"""The crawl task message: a versioned pointer to one URL of one job.

Wire format, as Redis stream entry fields (all strings):

=========  ==============================================================================
Field      Meaning
=========  ==============================================================================
``v``      the contract version, ``"1"``
``job_id`` the job's UUID
``url_id`` the crawl URL's UUID (``crawl_urls.id``)
=========  ==============================================================================

A message carries identifiers only. The worker reads everything else (the URL, its depth, its
state, its lease) from PostgreSQL, and acts on that state rather than on the message: a
duplicate, stale or late message can never cause more than a refused claim. Other fields are
ignored, so later versions may add fields that version 1 readers skip.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

MESSAGE_VERSION: Final = "1"
CONTRACT_FIELDS: Final = ("v", "job_id", "url_id")


class MalformedMessageError(ValueError):
    """A stream entry that is not a valid crawl task; it is dead-lettered, never processed."""


@dataclass(frozen=True, slots=True)
class CrawlTask:
    job_id: uuid.UUID
    url_id: uuid.UUID

    def encode(self) -> dict[str, str]:
        return {"v": MESSAGE_VERSION, "job_id": str(self.job_id), "url_id": str(self.url_id)}

    @classmethod
    def decode(cls, fields: Mapping[str, str]) -> "CrawlTask":
        """Validate stream entry fields; raises ``MalformedMessageError`` with the reason."""
        version = fields.get("v")
        if version != MESSAGE_VERSION:
            raise MalformedMessageError(f"unsupported message version {str(version)[:16]!r}")
        try:
            return cls(job_id=uuid.UUID(fields["job_id"]), url_id=uuid.UUID(fields["url_id"]))
        except KeyError as exc:
            raise MalformedMessageError(f"missing field {exc.args[0]!r}") from None
        except (ValueError, AttributeError, TypeError):
            raise MalformedMessageError("job_id and url_id must be UUIDs") from None
