# adjust — rollout notes

## Kit epic-25 (né au standard industriel)

Module créé directement au standard du playbook (`server/modules/README.md`) :
catalogue généré, error_map documentée, topologie déclarée + discovery.

### Sources du catalogue (fetch du 2026-07-21)

- **Officiel (autorité)** : Datascape metrics glossary
  (`https://help.adjust.com/en/article/datascape-metrics-glossary`). La page
  est rendue en JS ; le corps de l'article a été extrait du payload de données
  Next.js de la page (`pageProps.article.body`) et committé verbatim dans
  `glossary_body.md`. `build_official_fields.py` (committé, déterministe,
  sans réseau) le parse vers `official_fields.json` : 343 métriques concrètes
  + 35 dimensions (table Dimensions de la référence du endpoint reports).
  - 150 IDs du glossaire sont des TEMPLATES de requête paramétrés par les
    données du compte (`{event_slug}_...`, `..._{cohort_period}`,
    `subscription_{event_from}_to_{event_to}_...`) : non émis comme champs
    (énumération non déterministe), rapportés par le builder. La découverte
    dynamique des event-metrics via le endpoint officiel Events
    (`/reports-service/events`) est une story de suivi.
  - 3 IDs malformés dans la doc officielle (`general revenue_events_min/est/max`,
    espace dans l'ID) : skippés + rapportés, jamais « corrigés » silencieusement.
  - Les ranges (`conversion_1 to conversion_6`, `conversion_value_1 to
    conversion_value_63`, variantes skad_direct) sont EXPANSÉS un champ par ID.
- **Enrichissement (jamais l'autorité)** : Supermetrics
  `https://docs.supermetrics.com/docs/adjust-fields.md` (81 métriques /
  50 dimensions), snapshot NON committé. Les IDs Supermetrics portent un
  préfixe interne `datascape__` retiré par la commande `_fetch` enregistrée
  (sed) pour aligner sur les IDs officiels du Report Service.

### Commandes de génération (orchestrateur, local uniquement)

```
uv run python server/modules/adjust/catalog_sources/build_official_fields.py
curl -sL https://docs.supermetrics.com/docs/adjust-fields.md | sed 's/datascape__//g' > server/modules/adjust/catalog_sources/supermetrics.md
uv run python scripts/build_api_catalog.py --module adjust \
    --sources-dir server/modules/adjust/catalog_sources \
    --report server/modules/adjust/catalog_sources/fusion-report.json
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
```

Résultat committé : 378 champs, `drift_ids` VIDE, 15 exposed (les champs du
manifest) / 363 planned ; tiers 60 core / 56 standard / 262 advanced.
`enrichment_only_ids` = champs dérivés Supermetrics (variantes de dates,
`system_metadata.*`, instances de templates cohorte) — suspects documentés,
jamais émis.

### Vérifications API (doc officielle, 2026-07-21)

- Endpoint : `GET https://automate.adjust.com/reports-service/report`,
  `Authorization: Bearer <api_token>` (auth_type `api_key`, secret via Nango
  uniquement — AD-3).
- Les valeurs de métriques arrivent en CHAÎNES dans `rows[]` (coercion dans
  `_insert_raw_rows`) ; `attr_dependency` par ligne (droppé) ; `204` = rapport
  vide documenté (0 ligne, pas une erreur).
- Erreurs : statuts HTTP uniquement (400/401/403/429/503/504), aucun sous-code
  publié → `error_map` clé statut + `_error_map_note` (pattern klaviyo).
- Rate limit : 50 req/s par IP source, burst 100
  (`/en/api/rs-api/rate-limits`) → quota manifest conservateur 600 pts / 60 s.
- Topologie : niveau unique `app` ; discovery via
  `GET /reports-service/filters_data?required_filters=apps` → `[{id, name}]`.
  Aucune variable d'environnement de compte (standard 25.5+) : sans sélection,
  la pull reste scopée au compte du token (toutes ses apps, chaque ligne
  portant `app_token`).

### AD-4 (décisions croisées)

- `revenue` Adjust ajouté à `dbt/seeds/metric_source_priority.csv`
  (shopify 1, stripe 2, **adjust 3**) — couvert par `cross_source_revenue`.
- `sessions` Adjust vs GA4 : propriétés différentes, jamais sommées
  (discipline klaviyo, notes dans le manifest et le bloc mart).
- Ratios (`ctr`, `*_rate`) jamais stockés (drop dans `transform()`).

### Live probe / ratification (AI-13)

Pass live DIFFÉRÉE — aucun compte Adjust de test disponible (réalité
no-test-account, Jean 2026-07-21). Le module reste
`public_catalog.verification.status: blocked` jusqu'à une ratification réelle :

```
uv run python scripts/ratify_connector.py --module adjust \
    --connection <connection_ref_id> --account <app_token> --tier core
```

Points à vérifier en passe live (hypothèses encodées dans les mocks) :
format exact des valeurs (chaînes vs nombres), clé de réponse de
`filters_data?required_filters=apps` (`apps: [{id, name}]`), profondeur
d'historique de `date_period`, comportement `utc_offset` par défaut.
