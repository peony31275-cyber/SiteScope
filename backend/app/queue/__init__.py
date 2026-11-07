"""Task delivery over Redis Streams: message contracts and the stream adapter (Phase 5).

Redis only delivers work. Every message is a pointer to state in PostgreSQL, which stays the
source of truth (ADR-0001): a message never decides whether a URL is crawled, who holds its
lease, or how a level advances. This package has no domain logic and no database access.
"""
