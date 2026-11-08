"""Search document building and query handling: pure functions, no database or Redis (Phase 7).

* ``documents``: how a page becomes a search document (text-search configuration, field
  weights, size limits).
* ``queries``: how user input becomes a safe query (normalization, pagination bounds), and how
  highlighted snippets are decoded without HTML.

The services (``app/services/search_index.py``, ``app/services/search.py``) run the SQL.
"""
