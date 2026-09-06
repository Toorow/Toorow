{#-
  ============================================================================
  STEP 3 OF THE FX CUTOVER -- the resolution ORDER, in SQL.

  Ratified in docs/product-architecture/capabilities/currency-fx.md, section
  "Arbitration, 2026-08-21 -- the read path", step 3:

      "the resolution ORDER in SQL -- posed rate, window and condition first,
       seed as fallback -- carrying the specificity ranking, the tie REFUSAL and
       the three-valued UNRESOLVED verdict that core.fx_rate_sets._resolve_posed
       implements. [...] A plain LEFT JOIN would pick a row where the engine
       refuses, which is how bullets 9 and 10 return through the warehouse door."

  THIS IS A SECOND IMPLEMENTATION OF ONE RULE, AND IT IS ADMITTED AS ONE. The
  precedent the document names is `dbt/macros/fee_tax_condition_matcher.sql`
  standing beside `core.tax_fee_rule_set`, and what that precedent carries is a
  DIFFERENTIAL ORACLE, not two half-tested engines: the same case, posed to both,
  must give the same verdict. The oracle here is one case table --
  `dbt/seeds/fx_posed_rates_fixture.csv` and the `probe` CTE of
  `dbt/tests/test_fx_posed_rate_governs.sql` -- judged twice: `dbt build` runs
  this SQL against it, and
  `server/tests/core/test_fx_warehouse_resolution_matches_engine.py` runs
  `core.fx_rate_sets._resolve_posed` against the SAME table, parsed out of the
  same file so the two cannot be edited apart.

  ============================================================================
  WHY A MACRO AND NOT A MODEL.

  The condition key `connector` is answerable, and its answer differs per staging
  model -- `meta-ads` for one, `stripe` for the next. A model would have to
  enumerate the thirteen connector names in a list every new connector must be
  added to; the macro takes the name the caller already writes one line above, in
  its `fx_res.source_module` predicate. Same argument, same shape, and no shared
  file that grows a line per connector.

  It compiles thirteen times. That is the fee/tax matcher's bargain too: COMPILED
  TEXT with one definition site beats a data edge plus a registry.

  ============================================================================
  WHICH CONDITION KEYS A STAGING ROW CAN ANSWER -- measured, not assumed.

  `core.fx_fixed_rates.CONDITION_KEYS` admits four: connector, country, market,
  datastream. A staging row carries the first and NONE of the other three:
  `grep -l "datastream_id" server/modules/*/dbt/staging/*.sql` -> no file, and
  country/market are resolved in the marts (`fee_tax_country_resolution`,
  `int_country_daily_kpi`), one layer below where the rate is joined.

  So at THIS grain a rate conditioned on country, market or datastream is
  UNRESOLVED -- not NO_MATCH, and above all not a silent fall-through to the
  unconditional rate. That is bullet 11 of the capability
  ("a condition the figure cannot answer falls through to the unconditional rate
  instead of refusing") and it is the three-valued logic both engines already
  carry: UNRESOLVED beats NO_MATCH, because if one clause cannot be evaluated the
  conjunction is UNKNOWN, not false.

  An observation declaring a key with no readable value row is UNRESOLVED too
  (`_fx_cond.observation_id IS NULL` below), never quietly dropped -- fee/tax's
  rule verbatim: an unknown key is UNRESOLVED, never ignored.

  ============================================================================
  WHY ELEMENTARY INTERVALS, AND NOT A LEFT JOIN ON THE WINDOW.

  A posed rate holds over `[valid_from, valid_to]` and rates may overlap -- a
  conditional rate and the unconditional rate it refines MUST coexist for either
  to have a precedence (that is migration 288's first repair). So joining the
  staging row straight onto the candidate set fans it out, and collapsing the
  fan-out afterwards needs each model's own grain key: thirteen different lists,
  thirteen chances to write one wrong.

  Instead the candidates are cut at every boundary they declare -- each
  `valid_from`, and each `valid_to` plus one day -- which yields half-open
  segments `[seg_from, seg_until)` that are DISJOINT by construction and inside
  which the covering candidate set is constant. The winner is chosen once per
  segment. The caller's join can then be a plain LEFT JOIN and still return AT
  MOST ONE ROW, with no QUALIFY and no grain key.

  Half-open, so no day is claimed twice and no day falls between two segments.

  ============================================================================
  WORDING A, AND IT IS VISIBLE HERE.

  Decided 2026-08-22 by the capability document: the DECLARED WINDOW is the
  authority, and the posting date is not a boundary anywhere. Nothing below reads
  when a rate was posted -- only `[valid_from, valid_to]`. A posed rate therefore
  applies to every day inside its window, including days already published at
  another rate, and `fact_daily_kpi` being rebuilt in full on every run means a
  closed month may show a different total than it showed yesterday. That is what
  `fx_method = 'fixed'`, `fx_as_of_date` and the version's `created_by` exist to
  explain.
-#}


{% macro toorow_fx_day_after(col) %}
{#- The day after a DATE, in the two dialects this warehouse compiles to.

    The repo idiom for cross-adapter date arithmetic is an explicit `target.type`
    branch (`dbt/macros/days_between.sql`), not a defensive one: DuckDB and
    Postgres add an integer to a DATE, BigQuery requires DATE_ADD. Used for the
    upper boundary so the segments below can be half-open and no `-1` appears in
    a join predicate. -#}
{%- if target.type == 'bigquery' -%}
DATE_ADD({{ col }}, INTERVAL 1 DAY)
{%- else -%}
(CAST({{ col }} AS DATE) + 1)
{%- endif -%}
{% endmacro %}


{% macro toorow_fx_posed_resolution(connector_name, rates_relation=none, conditions_relation=none) %}
{#- The governed posed rate that governs each day, or nothing.

    Emits a parenthesised relation the caller LEFT JOINs on
    (project_id, base_currency, quote_currency) with
    `date >= seg_from AND date < seg_until`. At most one row per staging row.

    COLUMNS: project_id, base_currency, quote_currency, seg_from, seg_until,
             rate, effective_date, fx_gap_code, observation_id.

    `rate IS NULL AND fx_gap_code IS NULL` cannot occur: a segment exists only
    where a candidate covers it, and a covering candidate is either MATCH (a
    winner exists) or UNRESOLVED (a gap is named).

    Both mirror relations are read through `toorow_source_or_empty`: migration
    305 is applied by hand and `mirror_sync` defers its BigQuery writes, so a
    warehouse without them yields no segment, no gap, and the seed converts
    exactly as it did before the cutover.

    `rates_relation` / `conditions_relation` OVERRIDE those two sources, and they
    exist for ONE reason: a singular test must be able to pose the cases this
    engine is judged on -- a tie, an unanswerable key, a conditional rate beating
    the rate it refines -- and it cannot write into the mirror. Left at `none`
    (every production caller) the guarded mirror sources are read, so the
    override cannot silently change what a staging model converts at. The
    precedent is `fee_tax_condition_matcher`'s `conditions_relation`, passed by
    the caller for the same class of reason. -#}
{%- set rates = rates_relation if rates_relation is not none else toorow_source_or_empty('mirror', 'fx_posed_rates', [
        ['project_id', 'string'],
        ['observation_id', 'string'],
        ['base_currency', 'string'],
        ['quote_currency', 'string'],
        ['rate', 'numeric'],
        ['effective_date', 'date'],
        ['valid_from', 'date'],
        ['valid_to', 'date'],
        ['condition_key_count', 'int'],
    ]) -%}
{%- set conditions = conditions_relation if conditions_relation is not none else toorow_source_or_empty('mirror', 'fx_posed_rate_conditions', [
        ['observation_id', 'string'],
        ['condition_key', 'string'],
        ['condition_value', 'string'],
    ]) -%}
(
    WITH _fx_posed AS (
        SELECT
            project_id,
            observation_id,
            base_currency,
            quote_currency,
            rate,
            -- CAST on all three dates, not only on the arithmetic one: a seed
            -- fixture lands them as VARCHAR and a CASE arm of VARCHAR beside the
            -- seed's DATE `rate_date` is a type error in BigQuery rather than a
            -- coercion.
            CAST(effective_date AS DATE)                          AS effective_date,
            CAST(valid_from AS DATE)                              AS valid_from,
            CAST({{ toorow_fx_day_after('valid_to') }} AS DATE)   AS valid_until,
            condition_key_count
        FROM {{ rates }}
    ),

    -- One row per observation that declares a condition. Conjunction ACROSS
    -- keys, disjunction ACROSS the values of one key -- so `connector_matched`
    -- is a MAX over the values, and `foreign_key_declared` is a MAX over the
    -- keys this grain cannot answer.
    _fx_cond AS (
        SELECT
            observation_id,
            MAX(CASE WHEN condition_key <> 'connector' THEN 1 ELSE 0 END)
                AS foreign_key_declared,
            MAX(CASE WHEN condition_key = 'connector'
                      AND condition_value = '{{ connector_name }}'
                     THEN 1 ELSE 0 END)
                AS connector_matched,
            -- WHICH key could not be answered. A reader six months later must be
            -- able to say why this figure did not convert; "a condition failed"
            -- does not answer that. MIN is deterministic, not meaningful.
            MIN(CASE WHEN condition_key <> 'connector' THEN condition_key END)
                AS unresolved_key
        FROM {{ conditions }}
        GROUP BY observation_id
    ),

    -- The three-valued verdict of each posed rule against THIS connector.
    -- 0 = MATCH, 1 = NO_MATCH, 2 = UNRESOLVED, the ranks
    -- `core.fx_rate_sets._evaluate_condition` uses.
    _fx_eval AS (
        SELECT
            p.project_id,
            p.observation_id,
            p.base_currency,
            p.quote_currency,
            p.rate,
            p.effective_date,
            p.valid_from,
            p.valid_until,
            p.condition_key_count,
            CASE
                -- The unconditional rate. Not a special case -- the DEGENERATE
                -- one, which is what makes it the fallback rather than a rival.
                WHEN p.condition_key_count = 0            THEN 0
                -- A rule declares keys but none reached the warehouse: never
                -- ignored, always UNRESOLVED.
                WHEN c.observation_id IS NULL             THEN 2
                -- country / market / datastream: unanswerable at this grain.
                WHEN c.foreign_key_declared = 1           THEN 2
                WHEN c.connector_matched = 1              THEN 0
                ELSE 1
            END AS verdict,
            CASE
                WHEN p.condition_key_count = 0 THEN CAST(NULL AS {{ dbt.type_string() }})
                WHEN c.observation_id IS NULL  THEN 'condition_unreadable'
                ELSE c.unresolved_key
            END AS unresolved_key
        FROM _fx_posed p
        LEFT JOIN _fx_cond c ON c.observation_id = p.observation_id
    ),

    -- NO_MATCH rules are simply not candidates: they are the rules that do not
    -- govern this connector, and dropping them is not a silence.
    _fx_candidate AS (
        SELECT * FROM _fx_eval WHERE verdict <> 1
    ),

    -- Every date at which the covering candidate set can change.
    --
    -- `UNION ALL` + `SELECT DISTINCT`, and not a bare `UNION`: BigQuery answers
    -- *Expected keyword ALL or keyword DISTINCT but got keyword SELECT* to the
    -- unqualified form, which DuckDB accepts. Measured with a dry run over the
    -- compiled model (0 bytes), the 60.3 dialect obligation applied here.
    _fx_bound AS (
        SELECT DISTINCT project_id, base_currency, quote_currency, boundary
        FROM (
            SELECT project_id, base_currency, quote_currency, valid_from  AS boundary
            FROM _fx_candidate
            UNION ALL
            SELECT project_id, base_currency, quote_currency, valid_until AS boundary
            FROM _fx_candidate
        ) b
    ),

    _fx_ordered AS (
        SELECT
            project_id, base_currency, quote_currency, boundary,
            LEAD(boundary) OVER (
                PARTITION BY project_id, base_currency, quote_currency
                ORDER BY boundary
            ) AS boundary_next
        FROM _fx_bound
    ),

    _fx_segment AS (
        SELECT project_id, base_currency, quote_currency,
               boundary      AS seg_from,
               boundary_next AS seg_until
        FROM _fx_ordered
        WHERE boundary_next IS NOT NULL
    ),

    -- Full coverage, never partial: by construction of the boundary set a
    -- candidate either covers a whole elementary segment or none of it.
    _fx_covered AS (
        SELECT
            s.project_id, s.base_currency, s.quote_currency,
            s.seg_from, s.seg_until,
            c.observation_id, c.rate, c.effective_date,
            c.verdict, c.condition_key_count, c.unresolved_key
        FROM _fx_segment s
        JOIN _fx_candidate c
          ON  c.project_id     = s.project_id
          AND c.base_currency  = s.base_currency
          AND c.quote_currency = s.quote_currency
          AND c.valid_from    <= s.seg_from
          AND c.valid_until   >= s.seg_until
    ),

    -- UNRESOLVED IS DECIDED BEFORE THE RANKING, exactly as
    -- `_resolve_posed` raises on `unresolved` before it looks at `matched`. A
    -- more specific rule that matches does NOT rescue a segment where another
    -- rule cannot be evaluated: the figure might have been governed by that
    -- other rule, and serving a number anyway is the reassuring answer this
    -- capability refuses.
    _fx_state AS (
        SELECT
            project_id, base_currency, quote_currency, seg_from, seg_until,
            MAX(CASE WHEN verdict = 2 THEN 1 ELSE 0 END)          AS any_unresolved,
            MIN(CASE WHEN verdict = 2 THEN unresolved_key END)    AS unresolved_key,
            MAX(CASE WHEN verdict = 0 THEN condition_key_count END) AS best_specificity
        FROM _fx_covered
        GROUP BY project_id, base_currency, quote_currency, seg_from, seg_until
    ),

    -- The most specific MATCHING rule, and how many share that specificity.
    _fx_winner AS (
        SELECT
            c.project_id, c.base_currency, c.quote_currency,
            c.seg_from, c.seg_until,
            COUNT(*)                 AS winner_count,
            MIN(c.rate)              AS rate,
            MIN(c.effective_date)    AS effective_date,
            MIN(c.observation_id)    AS observation_id
        FROM _fx_covered c
        JOIN _fx_state g
          ON  g.project_id     = c.project_id
          AND g.base_currency  = c.base_currency
          AND g.quote_currency = c.quote_currency
          AND g.seg_from       = c.seg_from
          AND g.seg_until      = c.seg_until
        WHERE c.verdict = 0
          AND c.condition_key_count = g.best_specificity
        GROUP BY c.project_id, c.base_currency, c.quote_currency,
                 c.seg_from, c.seg_until
    )

    SELECT
        g.project_id                                    AS project_id,
        g.base_currency                                 AS base_currency,
        g.quote_currency                                AS quote_currency,
        g.seg_from                                      AS seg_from,
        g.seg_until                                     AS seg_until,
        -- A TIE IS REFUSED, NEVER BROKEN QUIETLY. `MIN(rate)` above would be a
        -- rate picked by row order, and a rate picked that way restates every
        -- monetary figure it touches and leaves nothing to notice it by. So when
        -- two equally specific rules match, no rate is served and the conflict
        -- is NAMED -- bullet 10 of the capability, written in SQL.
        CASE WHEN g.any_unresolved = 1
               OR COALESCE(w.winner_count, 0) > 1 THEN NULL
             ELSE w.rate END                            AS rate,
        CASE WHEN g.any_unresolved = 1
               OR COALESCE(w.winner_count, 0) > 1 THEN NULL
             ELSE w.effective_date END                  AS effective_date,
        CASE WHEN g.any_unresolved = 1              THEN 'fx_condition_unresolved'
             WHEN COALESCE(w.winner_count, 0) > 1   THEN 'fx_condition_conflict'
             ELSE CAST(NULL AS {{ dbt.type_string() }}) END
                                                        AS fx_gap_code,
        CASE WHEN g.any_unresolved = 1
               OR COALESCE(w.winner_count, 0) > 1 THEN CAST(NULL AS {{ dbt.type_string() }})
             ELSE w.observation_id END                  AS observation_id
    FROM _fx_state g
    LEFT JOIN _fx_winner w
      ON  w.project_id     = g.project_id
      AND w.base_currency  = g.base_currency
      AND w.quote_currency = g.quote_currency
      AND w.seg_from       = g.seg_from
      AND w.seg_until      = g.seg_until
)
{% endmacro %}


{% macro toorow_fx_evidence_columns(posed='fxp', seed='fx') %}
{#- THE SIX FX EVIDENCE COLUMNS a staging model emits, in one place.

    Thirteen staging models carried five of these six inline, and three of the
    five were LITERALS: `'seed' AS fx_source`, `'fixed' AS fx_tier`. Those
    literals were true only while the seed was the sole authority; the moment a
    posed rate can win, a literal `'seed'` on a governed rate is exactly the
    defect bullet 6 refuses ("a rate somebody POSED is presented under the label
    of an observed one"), read from the other side. So they become derived here,
    once, instead of thirteen times.

    THE ORDER IS THE RATIFIED ONE: posed rate first, seed as fallback. And a
    REFUSAL IS NOT A FALL-THROUGH -- when the posed step names a gap
    (`fx_condition_unresolved`, `fx_condition_conflict`) NO RATE IS SERVED, the
    seed included. Falling back to the seed there would be precisely bullet 11:
    a condition the figure cannot answer, resolved by serving another number.

    `fx_tier` and `fx_method` keep the distinction `money_evidence.sql` spells
    out: the tier says which RATE TABLE served the row, the method says how the
    number was OBTAINED. A posed rate is served by the governed store (`posed`)
    and was obtained by being declared (`fixed`).

    `toorow_exact_money_type()` on both branches, for two reasons: it is the exact
    type `fx_convert_at_read` multiplies at (38 digits, 9 fractional), and a CASE
    whose arms are NUMERIC and FLOAT64 is an error in BigQuery rather than a
    coercion. Never a literal `DECIMAL(38, 9)` -- BigQuery refuses a parameterised
    type in a CAST, which is what the macro exists to spell around. -#}
    CASE WHEN {{ posed }}.fx_gap_code IS NOT NULL THEN NULL
         WHEN {{ posed }}.rate IS NOT NULL THEN CAST({{ posed }}.rate AS {{ toorow_exact_money_type() }})
         ELSE CAST({{ seed }}.rate AS {{ toorow_exact_money_type() }})
    END                                            AS fx_rate,
    -- The day the rate was QUOTED or DECLARED -- never the day its window opens.
    -- Story 58.7 repaired that once for the seed (`fx.rate_date`, not
    -- `fx.valid_from`, which printed `2020-01-01` under every converted amount);
    -- the posed branch carries `effective_date` for the same reason.
    CASE WHEN {{ posed }}.fx_gap_code IS NOT NULL THEN NULL
         WHEN {{ posed }}.rate IS NOT NULL THEN {{ posed }}.effective_date
         ELSE {{ seed }}.rate_date
    END                                            AS fx_as_of_date,
    CASE WHEN {{ posed }}.fx_gap_code IS NOT NULL THEN NULL
         WHEN {{ posed }}.rate IS NOT NULL THEN 'governed'
         ELSE 'seed'
    END                                            AS fx_source,
    CASE WHEN {{ posed }}.fx_gap_code IS NOT NULL THEN NULL
         WHEN {{ posed }}.rate IS NOT NULL THEN 'posed'
         ELSE 'fixed'
    END                                            AS fx_tier,
    -- CARRIED, never minted -- on both branches. The seed's own `fx_method`
    -- column exists because until 2026-08-17 the warehouse stamped `direct` (the
    -- word reserved for an OBSERVED quotation) on a number a developer had
    -- typed. A governed posed rate is `fixed`, which is the label migration 279
    -- created so that a posed number could stop borrowing one.
    CASE WHEN {{ posed }}.fx_gap_code IS NOT NULL THEN NULL
         WHEN {{ posed }}.rate IS NOT NULL THEN 'fixed'
         ELSE {{ seed }}.fx_method
    END                                            AS fx_method,
    -- WHY THIS ROW HAS NO RATE, when the reason is a governance defect rather
    -- than an absence. NULL when the row converted, and NULL when it simply
    -- found no rate at all -- `money_gap_code` in the mart still says
    -- `fx_rate_unavailable` for that, which is the honest word for it.
    {{ posed }}.fx_gap_code                        AS fx_gap_code,
{% endmacro %}
