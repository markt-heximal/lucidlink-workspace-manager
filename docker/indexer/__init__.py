"""Delta indexer for LucidLink filespaces (the `lucidlink-indexer` container).

Crawls each configured filespace through the LucidLink SDK, extracts and chunks
text, embeds only what changed, and serves search / ask / survey over it. See
docs/INDEXER.md for the contract. Entry point: `uvicorn indexer.app:app`.
"""
