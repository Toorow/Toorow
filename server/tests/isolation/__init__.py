"""Cross-project isolation suite (Story 7.4, FR12, AD-5).

Turns "no query crosses project scope" from a promise into a verified property.
Every test provisions two isolated projects with DISTINCT data across the whole
AD-5 tree (Project -> Tool -> Auth -> Report -> Dimension) and asserts that a
request scoped to one project NEVER returns the other's data. All tests require
TEST_POSTGRES_DSN and are marked @pytest.mark.isolation.
"""
