# Brevo rollout notes

OAuth authorization code is the distributed default; enabled profiles determine the
union of minimum read scopes. Enterprise subaccounts and API-key mode are not silently
selected. Raw email/contact identifiers are excluded and landing uses project-salted
hashes. Live verification needs consent/refresh, scope introspection, all enabled
profiles, real quota headers, webhook signatures where enabled and the PII policy.
