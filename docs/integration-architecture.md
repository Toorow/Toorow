# Integration Architecture

## Runtime flow

```text
MCP client / agent
        |
        v
FastMCP server + inbound identity
        |---------------------------> Admin REST API <---- Admin React UI
        |
        +--> project/ACL resolution
        +--> module discovery and namespaced tools
        +--> shared queue / scheduler
                    |
                    +--> Nango (provider OAuth/token refresh)
                    +--> source API or Airbyte extraction
                    +--> immutable raw landing with pull_id
                                      |
                                      v
                              dbt semantic layer
                                      |
                                      v
                         canonical marts / read-through cache
                                      |
                                      v
                    short LLM summary + full widget envelope
```

## Storage ownership

- PostgreSQL owns project/access/configuration/governance state.
- DuckDB is the local analytical and cache surface.
- BigQuery is the intended production analytical owner.
- A governed mirror carries Postgres-owned context into analytics when needed;
  it does not create a second writer.

## Build integration

- pnpm builds design tokens before dependent UI packages.
- Vite produces self-contained HTML for widgets/cards.
- The bundle gate rejects external HTTP assets.
- dbt consumes module staging SQL and produces source-neutral marts.
- Docker packages the server and required workspace files.
- GitHub Actions gates deploy on CI success; production has a human environment
  approval and external secret/variable requirements.
