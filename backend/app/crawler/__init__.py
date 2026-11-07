"""Crawling building blocks that need no database: URL normalization and crawl scope.

Later phases add the safe fetcher, robots.txt handling, the rate limiter and the HTML parser here.
Nothing in this package imports ``app.db``, ``app.api`` or ``app.services``.
"""
