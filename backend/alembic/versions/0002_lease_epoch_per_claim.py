"""Fencing tokens are issued only by claims.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06

Closes two gaps in the database-enforced fencing of migration 0001 (ADR-0011). Migration 0001
only rejects a *decrease* of ``crawl_urls.lease_epoch``, so the database accepted:

* a claim or takeover that grants a lease without a new token. The previous holder's token stays
  valid, and a stale worker's correctly fenced commit (``WHERE lease_epoch = :token``) still
  matches the row;
* a stale worker that records its outcome under a newer epoch it issues to itself
  (``lease_epoch = lease_epoch + 1, result_epoch = lease_epoch + 1``), which satisfies every
  CHECK constraint.

The new trigger function ``sitescope_lease_epoch_per_claim`` enforces two transition rules:

* ``lease_epoch`` changes only in a claim: the row moves from ``queued`` or ``in_progress`` to
  ``in_progress``, and the token advances by exactly one;
* granting a lease, i.e. entering ``in_progress`` or handing an ``in_progress`` row to a different
  ``lease_owner``, requires that new token.

It runs as an AFTER trigger, after the CHECK constraints, so a statement that also violates a
CHECK still reports that constraint. The 0001 trigger that rejects decreases is unchanged.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION sitescope_lease_epoch_per_claim() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.lease_epoch <> OLD.lease_epoch THEN
                IF NEW.state <> 'in_progress'
                   OR OLD.state NOT IN ('queued', 'in_progress')
                   OR NEW.lease_epoch <> OLD.lease_epoch + 1 THEN
                    RAISE EXCEPTION
                        'lease_epoch of crawl URL % may only advance by one, in a claim'
                        ' (state % -> %, lease_epoch % -> %)',
                        OLD.id, OLD.state, NEW.state, OLD.lease_epoch, NEW.lease_epoch
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.state = 'in_progress'
                  AND (OLD.state <> 'in_progress'
                       OR NEW.lease_owner IS DISTINCT FROM OLD.lease_owner) THEN
                RAISE EXCEPTION
                    'granting the lease of crawl URL % requires a new lease_epoch'
                    ' (state % -> %, lease_epoch stays %)',
                    OLD.id, OLD.state, NEW.state, OLD.lease_epoch
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    # The WHEN clause skips updates that cannot grant a lease or change the token, so ordinary
    # updates (results, releases, publication) queue no trigger event at all.
    op.execute(
        "CREATE TRIGGER crawl_urls_lease_epoch_per_claim AFTER UPDATE ON crawl_urls"
        " FOR EACH ROW"
        " WHEN (NEW.lease_epoch <> OLD.lease_epoch OR NEW.state = 'in_progress')"
        " EXECUTE FUNCTION sitescope_lease_epoch_per_claim()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER crawl_urls_lease_epoch_per_claim ON crawl_urls")
    op.execute("DROP FUNCTION sitescope_lease_epoch_per_claim()")
