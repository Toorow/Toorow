# Taboola rollout notes

OAuth2 client credentials are brokered/cached by Nango for the 12-hour provider token.
KPI Summary/Top Content and immutable Campaign History use separate landings. Top
Content is explicitly capped at 1,000 rows. Dynamic conversion columns must be declared
by response metadata. Numeric quota remains validation-required; live 429 is authoritative.
