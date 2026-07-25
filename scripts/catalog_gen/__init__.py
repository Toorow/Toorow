"""catalog_gen — reusable tooling for generating api_catalog.json from source files.

Parsers and merge engine for the catalog generation pipeline (Story 25.3).
Imports:
    supermetrics_parser  — parse Supermetrics docs.supermetrics.com markdown snapshots
    discovery_parser     — parse Google discovery document JSON
    merge                — merge official + enrichment into a validated catalog
"""
