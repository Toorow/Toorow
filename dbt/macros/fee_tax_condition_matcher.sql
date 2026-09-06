{% macro fee_tax_condition_matcher(attributes_cte, candidate_pairs_cte, conditions_relation) %}
{#- Epic 41 -- THE ONE DEFINITION SITE of the three-valued row/rule condition matcher.

    EXTRACTED BY STORY 41.4 (ruling Q3) out of fee_tax_ladder_daily.sql, where it lived
    inline. It is emitted VERBATIM -- the extraction is behaviour-preserving by
    construction, not by hope: the four CTEs below are the same SQL text the ladder
    carried, with the two relation names it hard-coded turned into parameters. The gate
    is numeric (§A.3): 41.3's singular tests must stay green with byte-identical pinned
    integers, and feetax_dev_complete's live cascade must remain
    12345670000 -> 493826800 -> 0 -> 2265793553 -> 1510529035 -> 16615819388
    -> 3323163878 -> 19938983266.

    WHY IT IS A MACRO AND NOT A MODEL. Its output is FOUR CTEs, not one relation, and its
    consumers need them INSIDE their own WITH chain so the matcher sees each model's own
    candidate pairs. A model would also create a DATA edge between the cost ladder and the
    verification overlay, which Guard 1 of Story 41.4 (§D.7) exists to forbid. A macro is
    COMPILED TEXT, not a data edge, so the two sub-DAGs stay disjoint.

    ================================ EMITTED NAMES ==============================
    row_attributes_long . cond_eval . rule_state . cond_gap -- each terminated by a
    comma, so a caller drops the call where the CTEs used to be and every downstream
    reference (the ladder's `evaluated` and `all_gaps`; the overlay's equivalents) is
    untouched. DO NOT RENAME THEM: the names ARE the interface.

    ================================ INPUT CONTRACT =============================
      * `attributes_cte`      -- a CTE name. MUST expose: row_key, attr_connector,
                                 attr_country, attr_market, attr_source_type,
                                 attr_placement_type, attr_tax_code. Exactly one row per
                                 row_key.
      * `candidate_pairs_cte` -- a CTE name. MUST expose: row_key, rule_id. One row per
                                 (row x rule) pair the rule COULD govern (the caller owns
                                 the per-day effective window and the scope reach, because
                                 only the caller knows its own grain).
      * `conditions_relation` -- passed BY THE CALLER as
                                 source('mirror', 'fee_tax_rule_conditions'), NOT called
                                 inside this macro, so every consuming model keeps an
                                 explicit, greppable depends_on edge and the conformance
                                 tests can assert the dependency set PER MODEL.
    Anything missing from those lists is a CALLER bug, not a macro bug.

    ===================== NO target.type BRANCH, AND THAT IS DELIBERATE =========
    This macro emits only CASE / MAX / GROUP BY / UNION ALL / LEFT JOIN, which are
    identical in DuckDB and BigQuery. There is therefore NOTHING to branch on -- do NOT
    add a defensive target.type arm here (normalize_dimension.sql is the counter-example
    the repo already carries), and do not remove the branch from the money macros, where
    the parameterised decimal width genuinely needs it. Every §B.5 banned construct is
    likewise absent by construction, so no adapter surface is added.

    ===================== THE SEMANTICS (moved here WITH the code) ==============
    Vocabulary, shared with Story 41.2: MATCH (rank 0) . NO_MATCH (rank 1, the epic's
    "known false") . UNRESOLVED (rank 2). Precedence UNRESOLVED > NO_MATCH > MATCH,
    obtained free from MAX(rank).
    WHY UNRESOLVED BEATS NO_MATCH (or a reviewer will call it a bug): if one clause
    cannot be evaluated, the conjunction is UNKNOWN, not false. With country unknown,
    `country IN ('FR') AND connector = 'meta-ads'` is unknown even when the connector
    clause is false -- asserting "false" would let us silently skip a rule that might
    have applied.
    Semantics: conjunction ACROSS condition_keys, disjunction ACROSS the values of one
    key. A rule with NO condition row is UNCONSTRAINED and matches every row -- which is
    why the caller's join to rule_state is a LEFT JOIN with COALESCE(state_rank, 0): an
    INNER JOIN would silently delete every unconditional rule, i.e. most of them. (That
    COALESCE lives in the CALLER because the caller owns the pair list; this macro's job
    is to report a state only for the pairs that HAVE conditions.)
    The six recognised condition_keys are connector / country / market / source_type /
    placement_type / tax_code. ANYTHING ELSE is CONDITION_KEY_UNKNOWN -> rank 2, never
    ignored. placement_type and tax_code are ALWAYS NULL today: no dimension named
    `placement` exists anywhere in fact_daily_kpi and no connector emits a tax code, so a
    rule conditioned on either is always UNRESOLVED. That is fail-honest, not a defect.

    ===================== R4: THE MARKET / COUNTRY ASYMMETRY ====================
    41.2's frozen contract states that attr_market MAY BE NON-NULL WHILE attr_country IS
    NULL. So on one and the same row, a rule conditioned ('market','emea') evaluates
    MATCH (or NO_MATCH) normally while a rule conditioned ('country','FR') evaluates
    UNRESOLVED and raises its gap. This falls out for free BECAUSE THE STATE IS ROLLED UP
    PER (row_key, rule_id) AND NEVER PER ROW. Do not hoist an "is this row resolvable?"
    flag to row level: that would drag the market rule down with the country rule and
    silently kill a rule that was perfectly evaluable. This is the single most likely
    wrong implementation of a consuming model;
    test_epic41_market_country_asymmetry.sql pins it in both directions.

    ===================== THE VOCABULARY CONSEQUENCE (the point of Q3) ==========
    The six condition gap codes -- CONDITION_KEY_UNKNOWN, COUNTRY_UNRESOLVED,
    MARKET_UNRESOLVED, SOURCE_TYPE_UNRESOLVED, PLACEMENT_TYPE_UNRESOLVED,
    TAX_CODE_UNRESOLVED -- now have exactly ONE spelling site, so they CANNOT drift
    between the cost ladder, the verification overlay and 41.5's revenue model. Drift
    between copies of this logic would mean one surface computing a different total than
    another from the same rules, which is the exact failure Epic 41 exists to prevent,
    arriving by maintenance rather than by bug.
    Consumers today: fee_tax_ladder_daily (Story 41.3) and
    fee_tax_verification_allocation (Story 41.4). Story 41.5 consumes it READ-ONLY. A new
    condition key is a change to THIS file and must re-run 41.3's byte-identity gate. -#}

-- Exactly SIX rows per input row, one per recognised attr_key, value possibly NULL.
-- That is what lets the matcher's LEFT JOIN distinguish "the attribute exists but
-- could not be resolved" (attr_value IS NULL -> rank 2) from "the rule references a
-- key we do not know at all" (attr_key IS NULL -> rank 2, CONDITION_KEY_UNKNOWN).
-- Neither is ever silently ignored.
row_attributes_long AS (
    SELECT row_key, 'connector'      AS attr_key, attr_connector      AS attr_value FROM {{ attributes_cte }}
    UNION ALL
    SELECT row_key, 'country'        AS attr_key, attr_country        AS attr_value FROM {{ attributes_cte }}
    UNION ALL
    SELECT row_key, 'market'         AS attr_key, attr_market         AS attr_value FROM {{ attributes_cte }}
    UNION ALL
    SELECT row_key, 'source_type'    AS attr_key, attr_source_type    AS attr_value FROM {{ attributes_cte }}
    UNION ALL
    SELECT row_key, 'placement_type' AS attr_key, attr_placement_type AS attr_value FROM {{ attributes_cte }}
    UNION ALL
    SELECT row_key, 'tax_code'       AS attr_key, attr_tax_code       AS attr_value FROM {{ attributes_cte }}
),

-- ----------------------------------------- D.3.2 RELATIONAL CONDITION MATCH --
cond_eval AS (
    -- One row per (input row, rule, condition_key): does ANY declared value match?
    SELECT
        p.row_key                                                          AS row_key,
        p.rule_id                                                          AS rule_id,
        c.condition_key                                                    AS condition_key,
        MAX(CASE WHEN c.condition_value = a.attr_value THEN 1 ELSE 0 END)  AS any_value_matched,
        MAX(CASE WHEN a.attr_value IS NULL             THEN 1 ELSE 0 END)  AS attr_unresolved,
        MAX(CASE WHEN a.attr_key   IS NULL             THEN 1 ELSE 0 END)  AS key_unknown
    FROM {{ candidate_pairs_cte }} p
    JOIN {{ conditions_relation }} c
        ON c.rule_id = p.rule_id
    LEFT JOIN row_attributes_long a
        ON  a.row_key  = p.row_key
        AND a.attr_key = c.condition_key
    GROUP BY p.row_key, p.rule_id, c.condition_key
),

rule_state AS (
    -- Roll the per-key verdicts up to ONE state PER (row, rule) -- never per row (R4).
    -- rank 0 = MATCH, 1 = NO_MATCH, 2 = UNRESOLVED; MAX() gives the precedence free.
    SELECT
        row_key,
        rule_id,
        MAX(CASE
                WHEN key_unknown       = 1 THEN 2   -- an unknown key is NEVER ignored
                WHEN attr_unresolved   = 1 THEN 2
                WHEN any_value_matched = 0 THEN 1
                ELSE 0
            END) AS state_rank
    FROM cond_eval
    GROUP BY row_key, rule_id
),

cond_gap AS (
    -- Which typed gap each unevaluable clause raises. Kept per (row, rule, key) so
    -- the country and market flags move INDEPENDENTLY (R4).
    SELECT
        row_key,
        rule_id,
        CASE
            WHEN key_unknown = 1                                     THEN 'CONDITION_KEY_UNKNOWN'
            WHEN attr_unresolved = 1 AND condition_key = 'country'        THEN 'COUNTRY_UNRESOLVED'
            WHEN attr_unresolved = 1 AND condition_key = 'market'         THEN 'MARKET_UNRESOLVED'
            WHEN attr_unresolved = 1 AND condition_key = 'source_type'    THEN 'SOURCE_TYPE_UNRESOLVED'
            WHEN attr_unresolved = 1 AND condition_key = 'placement_type' THEN 'PLACEMENT_TYPE_UNRESOLVED'
            WHEN attr_unresolved = 1 AND condition_key = 'tax_code'       THEN 'TAX_CODE_UNRESOLVED'
            ELSE NULL
        END AS gap_code
    FROM cond_eval
),
{% endmacro %}
