"""toorow — THE ONE PLACE A NARRATIVE SENTENCE IS SPELLED.

Ratified in `docs/product-architecture/analyze-and-test.md`, amendment
2026-08-25 (« how a narrative sentence is rendered »), which is the mechanical
form of Jean's arbitration of 2026-08-22 (« La langue d'un récit ») :

    Un récit se rend dans la LANGUE DU LECTEUR, et cette langue n'est donc
    jamais une constante du code. Une phrase de récit écrite en dur, quelle que
    soit sa langue, est le défaut -- l'anglais en dur l'était déjà, le traduire
    le déplace.

WHAT THAT LEAVES TO THE CODE, AND WHAT IT TAKES AWAY. A call site keeps the
FORM of a sentence -- which facts it states, in which order, with which
citation. It loses the WORDS. It names a phrase by a stable English KEY and
hands over named fields; this module decides how that key reads, in the language
asked for.

    lines.append(phrase("keywords_total_clicks", value=..., citation=...))

THE MECHANISM IS COMMIT 0472efaf's, WIDENED. That commit closed the client-label
family with « une map, deux consommateurs » : one resolved object handed to both
the heading and the sentence, so the two could not disagree. Here it is one
catalogue handed to every narrative surface -- `narrative`, `anomaly_alerts`,
`analyze_render_mcp`, `summarizer`, `reports` -- so no two of them can spell one
sentence two ways, and none of them can spell a new one at all.

IT IS CODE, ON PURPOSE. `CLAUDE.md` : « Un catalogue livré avec le produit ne se
lit pas dans une table ». The words ship with the build; nothing reads a row to
find them.

THE READER'S LANGUAGE — THE OPEN QUESTION, AND THE DEFAULT THAT IS NOT A GUESS.
The arbitration says the request decides at render time. NO REQUEST TRANSPORTS A
LANGUAGE TODAY (not `get_card`, not `get_report`, not the analyze tools), and
which request field should carry it is a product question this module does not
answer -- it is written as an open question in the surface document.

So the default is the CURRENT BEHAVIOUR, named rather than implied:
``DEFAULT_NARRATIVE_LANGUAGE``. And the catalogue carries exactly the languages a
request can actually ask for -- today, one. A second column is NOT pre-written:
an unreachable translation is a call site that looks armed and is not, which is
the trap 0472efaf refused for `build_narrative`. The day a request carries a
language, adding it is adding a column and threading one parameter; not one call
site changes.

BYTE-IDENTICAL. Every template below is the sentence the product shipped on
2026-08-24, moved and not rewritten. The card fixtures and the pinned tests read
exactly what they read before -- that is how a move can be proved to be a move.

This module IMPORTS NOTHING. `narrative.py` may read it without gaining an inch
of data access (AD-1, `scripts/check_narrative_no_raw.py`).
"""

from __future__ import annotations

#: The language a narrative is rendered in when the request names none.
#:
#: THIS IS THE ABSENCE OF A DEDUCTION, NOT A DEDUCTION. The surface document
#: forbids deriving the reader's language from anything but the request; until a
#: request carries one, a default has to be declared somewhere, and here it is,
#: in one line, set to what the product renders today.
DEFAULT_NARRATIVE_LANGUAGE = "fr"

#: The languages the catalogue actually ships. One entry, and the reason is in
#: the module docstring: a column no request can ask for is dead copy.
SUPPORTED_NARRATIVE_LANGUAGES = ("fr",)


# ---------------------------------------------------------------------------
# THE CATALOGUE.
#
# key   -- stable, English, lowercase_with_underscores. It says WHAT the sentence
#          states, never how it reads: a copy change must not move a key, or
#          every call site becomes a copy decision again.
# value -- {language: template}. `str.format` named fields only; a positional
#          `{}` would tie the words to an argument order.
# ---------------------------------------------------------------------------

PHRASES: dict[str, dict[str, str]] = {
    # -- Citations and absences (AD-9: an absent source is stated, never faked)
    "citation_unavailable": {"fr": "(contexte manquant)"},
    "context_missing": {"fr": "Contexte manquant pour cette période."},
    # AI-344: the events were not READ -- distinct from "read, and none". The
    # reason and the repair ride the envelope beside this line.
    "context_unavailable": {
        "fr": "Contexte non lu pour cette période : les événements n'ont pas pu être consultés."
    },
    "as_of_reconstituted": {"fr": "Données reconstituées au {as_of}."},
    "narrative_truncated": {
        "fr": "[contexte tronqué - {event_count} événements disponibles]"
    },
    # -- The what-section: one line per metric
    "delta_vs_period": {"fr": " ({delta_pct} vs {period})"},
    "metric_line": {"fr": "{label}: {value}{delta} {citation}"},
    "metric_combination_refused": {
        "fr": "{label}: pas de total combiné ({refusal}){detail} {citation}"
    },
    "metric_combination_detail": {"fr": " -- {detail}"},
    "metric_per_source": {"fr": "{name} {value}"},
    # -- The why-section: context events and alerts
    "context_event_line": {"fr": "{date} — {label}{detail} {citation}"},
    "context_event_detail": {"fr": ": {description}"},
    "alert_line": {"fr": "⚠ {subject}: {message} {citation}"},
    "alert_subject_fallback": {"fr": "alerte"},
    "cause_line": {"fr": "Contexte : {date} — {label} {citation}"},
    # -- The scope of the attachment (migration 322). A "Why" section asserts a
    # relationship between an event and the claim above it; these two lines say
    # on what that relationship was checked, and on what it was not. They state
    # dimension names, never a cause.
    "context_scope_line": {"fr": "Rattachement du contexte — comparé : {basis}."},
    "context_scope_unscoped": {"fr": " Non comparé : {unscoped}."},
    # -- Markets (story 37.8 / 48.2): a grouping is never named like a country
    "market_rest_of_world": {"fr": "Reste du monde"},
    "market_unknown": {"fr": "Géographie non résolue"},
    "market_split_line": {"fr": "Marché « {label} » : {value} {citation}"},
    "market_split_grouping_line": {"fr": "{label} : {value} {citation}"},
    # -- Keywords card
    "keywords_top_mover": {
        "fr": "{word} « {label} » : {direction} de {sign}{delta} pos. {citation}"
    },
    "keywords_mover_unquantified": {"fr": "{word} « {label} » en mouvement. {citation}"},
    "keywords_total_clicks": {"fr": "Clics totaux : {value}. {citation}"},
    "keywords_impressions": {"fr": "Impressions: {value}{delta} {citation}"},
    "keywords_cannibalisation": {
        "fr": "Cannibalisation : {word} « {label} » en concurrence sur "
        "{page_count} pages. {citation}"
    },
    "direction_gain": {"fr": "gain"},
    "direction_loss": {"fr": "perte"},
    # -- Conversions card
    "conversions_top_source": {
        "fr": "Source principale : « {label} » ({pct} des conversions) {citation}"
    },
    "conversions_total": {"fr": "Conversions totales : {value}. {citation}"},
    "conversions_cpa_with_delta": {
        "fr": "CPA : {value}{unit} ({delta} vs période précédente) {citation}"
    },
    "conversions_cpa_no_prior": {
        "fr": "CPA : {value}{unit} (variation du CPA indisponible : "
        "période précédente manquante) {citation}"
    },
    # -- User types card
    "usertypes_dominant_segment": {
        "fr": "{word} dominant : « {label} » ({pct} des utilisateurs actifs) {citation}"
    },
    "usertypes_active_total": {"fr": "Utilisateurs actifs : {value}. {citation}"},
    "usertypes_geography_grouping": {
        "fr": "{label} : {value} utilisateurs actifs {citation}"
    },
    "usertypes_top_market": {
        "fr": "Marché principal : « {label} » ({value} utilisateurs actifs) {citation}"
    },
    "usertypes_top_country": {
        "fr": "Premier {word} : « {label} » ({value} utilisateurs actifs) {citation}"
    },
    "usertypes_sessions": {"fr": "Sessions : {value}{delta} {citation}"},
    # -- Journey card
    "journey_worst_step": {
        "fr": "Abandon principal : étape « {label} » (taux de passage {rate}) {citation}"
    },
    "journey_sessions_total": {"fr": "Sessions totales : {value}. {citation}"},
    "journey_top_landing_page": {
        "fr": "{word} principale : « {label} » ({sessions} sessions) {citation}"
    },
    "journey_overall_rate": {"fr": "Taux de conversion global : {rate}. {citation}"},
    "journey_conversions": {"fr": "Conversions : {value}{delta} {citation}"},
    # -- Connectors card (an inventory: no cause line, ever)
    "connectors_none": {"fr": "Aucun connecteur configuré pour ce projet."},
    "connectors_count_one": {"fr": "{count} connecteur configuré pour ce projet."},
    "connectors_count_many": {"fr": "{count} connecteurs configurés pour ce projet."},
    "connectors_least_fresh": {
        "fr": "Connecteur le moins frais : « {name} » (dernière extraction le {date})."
    },
    "connectors_least_fresh_never": {
        "fr": "Connecteur le moins frais : « {name} » (jamais extrait)."
    },
    # -- Attribution card (AD-9: a gap is an observation, never a cause)
    "attribution_last_click_leader": {
        "fr": "{word} leader (dernier clic) : « {label} » "
        "({conversions} conversions{share}) {citation}"
    },
    "attribution_share_fragment": {"fr": ", {pct} % du top-N dernier clic"},
    "attribution_stronger_last": {"fr": "plus fort en dernier clic"},
    "attribution_stronger_first": {"fr": "plus fort en premier clic"},
    "attribution_gap": {
        "fr": "Écart dernier/premier clic pour « {label} » : {last_pct} % "
        "(dernier clic) vs {first_pct} % (premier clic) — {direction}. {citation}"
    },
    "attribution_gap_not_significant": {
        "fr": "Premier clic disponible ({value_count} valeurs) — "
        "écart non significatif vs dernier clic. {citation}"
    },
    # -- Deduplication card (AD-9 / epic 17: the word « estimation » is verbatim)
    "dedup_formula": {"fr": "rate = Σ claimed ÷ actual"},
    "dedup_source_fragment": {"fr": ", source : {source}"},
    "dedup_event_fragment": {"fr": " (event: {event})"},
    "dedup_coverage_partial": {
        "fr": "sur {covered} des {claimed} jours de la fenêtre couverts par la "
        "source de vérité"
    },
    "dedup_coverage_days": {"fr": "sur {covered} jours"},
    "dedup_rate_label": {"fr": "Taux de duplication estimé : {rate}"},
    "dedup_rate_coverage": {"fr": " ({coverage})"},
    "dedup_estimation": {"fr": "Estimation : {rate_label} ({formula}{source}) {citation}"},
    "dedup_estimation_unavailable": {
        "fr": "Estimation indisponible : source de vérification absente ou non "
        "configurée ({formula}) {citation}"
    },
    "dedup_leading_channel": {
        "fr": "Canal leader (estimation dédupliquée) : « {label} » "
        "({conversions} conversions, {share} du réel) {citation}"
    },
    "dedup_measured_coverage": {
        "fr": "Rapprochement par transaction (mesuré) : couverture {pct} {citation}"
    },
    # -- Mediaplan pacing card (AD-9 / epic 22)
    "pacing_formula": {"fr": "Pace = (actual − planned to-date) / planned to-date"},
    "pacing_citation": {"fr": "(plan version {version}, mart plan_pacing_by_line)"},
    "pacing_citation_with_pull": {
        "fr": "(plan version {version}, mart plan_pacing_by_line, pull {pull_id})"
    },
    "pacing_as_of_unavailable": {"fr": "(date indisponible)"},
    "pacing_coverage_days": {"fr": ", {days} jour(s) de réel"},
    "pacing_plan_header": {
        "fr": "Plan « {plan} » — arrêté au {as_of}{coverage} ({formula}) {citation}"
    },
    "pacing_amount": {"fr": "{amount} €"},
    "pacing_amount_unavailable": {"fr": "n/d"},
    "pacing_overall": {
        "fr": "Pace global : {sign}{pct} % (réel {actual} / budget {budget}) — "
        "Estimation par extrapolation (run-rate linéaire)"
    },
    "pacing_plan_only": {"fr": "lignes « plan seul » (pace non calculé): {lines}"},
    "pacing_plan_only_standalone": {
        "fr": "Lignes « plan seul » (pace non calculé): {lines} {citation}"
    },
    "pacing_plan_only_overflow": {"fr": " (+ {count} autres)"},
    "pacing_line_both": {"fr": "{pace} ; {plan_only} {citation}"},
    "pacing_line_single": {"fr": "{part} {citation}"},
    # -- How operator commentary is framed on the model channel (story 53.5,
    #    CAV-18). Shared by `cards._build_summary` and
    #    `reports._build_report_summary`, so the two cannot drift on the wording
    #    that carries the constraint.
    "operator_guidance_frame": {
        "fr": "Operator commentary guidance (an instruction, NOT evidence -- it "
        "must not become a cause, a figure or a citation the comment above does "
        "not support): "
    },
    # -- Anomaly alerts. TWO LANGUAGES LIVED IN THAT MODULE (a French text-channel
    #    line at `format_anomaly_line`, an English one at `fetch_recent_firings`)
    #    -- one alert language per product, so both keys resolve here.
    "anomaly_context_labels": {"fr": "Contexte : {labels}"},
    "anomaly_context_labels_period": {"fr": "Contexte : {labels}."},
    "anomaly_context_missing": {"fr": "Contexte manquant."},
    # AI-344: the walk did not run -- said as such, never as "manquant", which
    # would claim the events were read and none matched.
    "anomaly_context_unavailable": {
        "fr": "Contexte non lu (les événements n'ont pas pu être consultés -- {repair})"
    },
    "anomaly_line": {"fr": "⚠️ Anomalie : {metric} ({zscore}) le {date} -- {context}"},
    "anomaly_level_high": {"fr": "élevé"},
    "anomaly_level_low": {"fr": "bas"},
    "anomaly_widget_message": {"fr": "{metric} anormalement {level} ({zscore}). {context}"},
    "anomaly_firing_message": {
        "fr": "Anomalie sur {metric} (observé {observed}, référence {baseline})"
    },
    # -- The analyze text channel (`analyze_render_mcp.compact_answer`)
    "result_outcome": {"fr": "Resultat {result_id} - issue : {outcome}."},
    "result_counts": {
        "fr": "Lignes : {row_count} ; cellules : {cell_count} ; tronque : {truncated}."
    },
    "result_truncated_yes": {"fr": "oui"},
    "result_truncated_no": {"fr": "non"},
    "result_query_spec_version": {"fr": "Version du Query Spec : {version_id}."},
    "result_ai_path": {"fr": "Parcours IA : {ai_path}."},
    "result_content_hash": {"fr": "Empreinte du contenu : {content_hash}."},
    "result_freshness": {"fr": "Fraicheur : {state}{as_of}."},
    "result_freshness_as_of": {"fr": " (au {as_of})"},
    "result_provenance": {"fr": "Provenance : vue semantique {version_id}."},
    "result_missing_link": {
        "fr": "Limitation : la reponse est incomplete car {missing_link} est manquant."
    },
    "result_no_rows": {
        "fr": "Aucune ligne produite ; le manifeste ci-dessus indique pourquoi, et "
        "les compteurs sont rapportes comme inconnus plutot que zero."
    },
    "result_more_rows": {
        "fr": "Il existe plus de lignes que ce Resultat n'en stocke ; paginez dans "
        "le Resultat."
    },
    "result_not_reconciled": {
        "fr": "Limitation : ce Resultat n'a pas pu etre compare au total declare de "
        "sa mesure. Il n'est pas reconcilie."
    },
    "result_reconciliation": {
        "fr": "Par rapport au total declare de {measure} : total {total}, ce "
        "Resultat {breakdown_sum}, ecart {gap} ({verdict}). {statement}"
    },
    "result_reconciliation_withheld": {
        "fr": "{count} autre(s) mesure(s) de ce Resultat portent un total declare ; "
        "ouvrez le Resultat pour les lire."
    },
    "result_slice_loaded": {"fr": "Une page bornee du Resultat fige a ete chargee."},
    # -- The daily-report summarizer
    "summary_daily_report_header": {"fr": "Rapport quotidien — du {start} au {end}"},
    "summary_connectors": {"fr": "Connecteurs : {connectors}"},
    "summary_connectors_all": {"fr": "tous"},
    "summary_metric_line": {"fr": "- {label} : {value}"},
    "summary_connectors_overflow": {
        "fr": "… et {count} connecteur(s) supplémentaire(s)"
    },
    "summary_freshness": {"fr": "Fraîcheur : {value}"},
    "summary_pull_id": {"fr": "Pull ID : {pull_id}"},
    "summary_truncated": {
        "fr": "[tronqué — voir structuredContent pour le détail complet]"
    },
    "summary_project": {"fr": "Projet : {project_id}"},
    "summary_period": {"fr": "Période : {start} — {end}"},
    "summary_as_of_known": {"fr": "Données telles que connues le {as_of}"},
    "summary_as_of_source_currency": {
        "fr": "Avertissement : coûts en devise source (non normalisés) dans la vue "
        "as-of."
    },
    "summary_context_events_heading": {"fr": "Événements de contexte :"},
    "summary_context_event_item": {"fr": "- {date} [{type}] {label}"},
    "summary_context_events_overflow": {"fr": "... et {count} autre(s)"},
    "summary_context_none": {"fr": "Contexte : aucun événement connu pour cette période."},
    "summary_no_data": {"fr": "Aucune donnée disponible pour la période demandée."},
    # -- Report-level narrative notes (reports.py)
    "report_partial_data_note": {"fr": " [Données partielles : {metrics} non disponibles.]"},
    "report_no_deploy_events": {
        "fr": " Aucun déploiement trouvé dans la fenêtre. Contexte manquant."
    },
    "report_deploy_events_unavailable": {
        "fr": " Les déploiements de la fenêtre n'ont pas pu être lus : contexte inconnu."
    },
    # -- The confidence disclosure (AI-297). `reports._state_the_limiting_term`
    # spelled these two in place; the sentence names WHICH term holds a report
    # back, and the second half names the terms nothing could measure.
    "report_limiting_term": {"fr": "Confiance limitee par : {term}"},
    "report_limiting_term_value": {"fr": " ({value})"},
    "report_limiting_term_unmeasured": {"fr": " -- non mesure : {terms}"},
    # -- The MCP App's answer envelope (`analyze_render_mcp.build_summary`).
    # These reach a READER through the model channel, so they are narrative like
    # any other sentence -- they were simply spelled where they were used.
    #
    # `lineage_not_recorded` says WHY an old Result names no source: it was
    # frozen before `capture_evidence` recorded one. That is a different fact
    # from "this execution could not name its source", and the two must not
    # collapse into one sentence.
    "lineage_not_recorded": {"fr": "published before the lineage was recorded"},
    "result_counts_unknown": {"fr": "issue {outcome} ; aucune ligne produite"},
    "result_how_to_page": {
        "fr": "Appelez app_read_result_slice avec le result_handle de _meta, un "
        "offset ou un curseur et une limite d'au plus 500 lignes."
    },
    # -----------------------------------------------------------------------
    # THE CARD CATALOGUE'S OWN WORDS (`core/cards.py`).
    #
    # A card's title, the question it says it answers, the heading of each block
    # it composes and the labels of the columns it prints are all read by a
    # PERSON. They were spelled where they were used -- half in English, half in
    # French, none of them anywhere a translation could reach. Rule D counts 77
    # of them on 2026-08-31, and counted zero the day before only because it did
    # not look inside `cards.py` at all.
    #
    # WHERE THESE RESOLVE, SAID PLAINLY. Most are read inside a rendering
    # function. Those belonging to the declarative registry (`CARD_TEMPLATES`
    # and the module-level label maps) resolve AT IMPORT, in the one language
    # the catalogue ships. That is exact today -- `SUPPORTED_NARRATIVE_LANGUAGES`
    # has a single entry -- and it is the thing to move the day a request
    # carries a language: the registry would then hold the KEY and let its
    # reader resolve it. Written as an open point in the surface document rather
    # than left to be discovered.
    # -- Card titles, and the question each card says it answers
    "card_kpi_title": {"fr": "KPI Overview"},
    "card_kpi_question": {
        "fr": "How are my key performance indicators evolving over the period "
        "(values, variations, trend)?"
    },
    "card_keywords_question": {
        "fr": "How are my keywords / queries performing "
        "(top queries, rank movers, opportunities)?"
    },
    "card_conversions_question": {
        "fr": "Where do my conversions come from and at what cost (by source, CPA)?"
    },
    "card_usertypes_title": {"fr": "User Types"},
    "card_usertypes_question": {
        "fr": "Who are my users (new vs returning, device, country)?"
    },
    "card_journey_title": {"fr": "User Journey"},
    "card_journey_question": {
        "fr": "Where do users drop off (funnel steps, completion rates)?"
    },
    "card_attribution_question": {
        "fr": "What share of my conversions comes from each channel, "
        "last click vs first click?"
    },
    "card_videos_question": {
        "fr": "What did my videos do, and what changed when I published one?"
    },
    "card_dedup_question": {
        "fr": "Are my conversions being double-counted by ad platforms?"
    },
    "card_mediaplan_pacing_title": {"fr": "Media Plan Pacing"},
    "card_mediaplan_pacing_question": {
        "fr": "How is my media plan progressing against actual spend "
        "(consumed, pace, remaining, extrapolated by line and by channel)?"
    },
    "card_connectors_question": {
        "fr": "Which connectors are available and what do they feed?"
    },
    # -- Block headings. `{dimension}` is filled with the client's own word for a
    # dimension (`narrative.dimension_word`), never with a canonical identifier.
    "block_dimension_in_movement": {"fr": "{dimension} in movement (−pos.)"},
    "block_conversions_by_source": {"fr": "Conversions by source"},
    "block_cpa_vs_target": {"fr": "CPA vs target"},
    "block_by_source": {"fr": "By source"},
    "block_new_vs_returning": {"fr": "New vs returning"},
    "block_users_by_dimension": {"fr": "Users by {dimension}"},
    "block_sessions_to_conversions_funnel": {"fr": "Sessions -> conversions funnel"},
    "block_entry_pages_default": {"fr": "Entry pages"},
    "block_top_viewed_dimension": {"fr": "Top viewed {dimension}"},
    "block_channels_last_click": {"fr": "Channels — last click (top-N)"},
    "block_channels_first_click": {"fr": "Channels — first click (top-N)"},
    "block_dimension_last_click": {"fr": "{dimension} — last click (top-N)"},
    "block_views_and_publications": {"fr": "Views, and when you published"},
    "block_top_videos": {"fr": "Top videos"},
    "block_duplication_rate": {"fr": "Duplication rate (estimate)"},
    "block_duplication_rate_estimate": {"fr": "Taux de duplication (Estimation)"},
    "block_claimed_vs_deduplicated": {"fr": "Claimed vs deduplicated by channel"},
    "block_claimed_vs_deduplicated_estimate": {
        "fr": "Claimed vs deduplicated by channel (Estimate)"
    },
    "block_channel_detail": {"fr": "Channel detail"},
    "block_breakdown_by_channel": {"fr": "Breakdown by channel"},
    "block_plan_lines": {"fr": "Plan lines"},
    "block_rollup_by_channel": {"fr": "Rollup by channel"},
    "block_channel_rollup": {"fr": "Channel rollup"},
    "block_unmapped_actuals": {"fr": "Unmapped actuals"},
    # -- Column and series labels a table or a bar prints
    "column_average_position": {"fr": "Average position"},
    "column_last_extract": {"fr": "Dernier extrait"},
    "column_datastreams_feeding": {"fr": "Datastreams feeding"},
    "column_actual_spend": {"fr": "Actual spend (€)"},
    "column_plan_only": {"fr": "Plan only"},
    "series_deduplicated_contribution": {"fr": "Deduplicated contribution (Estimate)"},
    # -- Status words of the connectors card
    "connector_status_error": {"fr": "En erreur"},
    # -- Honest empty states and refusals. AD-9: each states what is missing and
    # names the gesture that fills it; none of them states a cause.
    "topic_no_readable_knowledge": {
        "fr": "This topic answers only from the project's governed knowledge, and "
        "none of the knowledge it declares can be read."
    },
    "markers_none_in_window": {
        "fr": "No release lands in this period, so the curve carries no marker."
    },
    "markers_off_axis": {
        "fr": "The releases of this period fall on days the curve does not draw, "
        "so none could be placed on it."
    },
    "movers_no_previous_period": {"fr": "No previous period available."},
    "cannibalisation_none_detected": {"fr": "No cannibalization detected"},
    "connectors_inventory_unavailable": {
        "fr": "\n(inventory unavailable: database unreachable)"
    },
    "dedup_no_verification_source_configured": {
        "fr": "No verification source designated for this project. "
        "Configure a source in the admin console "
        "to enable estimated deduplication."
    },
    "dedup_verification_source_unavailable": {
        "fr": "Verification source unavailable (governed data not available "
        "or a 'stripe' source not yet delivered)."
    },
    "dedup_no_verification_source": {"fr": "No verification source designated."},
    "dedup_source_unavailable_null_reason": {"fr": "source indisponible"},
    "dedup_honesty_note": {
        "fr": "aggregate estimate, never an exact measurement (without a user-level join "
        "BigQuery CAP-14/AD-14)"
    },
    "plan_scope_not_verifiable": {"fr": "Scope not verifiable (warehouse unavailable)"},
    "plan_pacing_honesty_note": {
        "fr": "extrapolated_spend is an Estimate (linear run-rate); "
        "jamais une mesure exacte. pace NULL = allocation to-date nulle ou ligne plan seul."
    },
    # -- The text-channel summary of a rendered card (AD-1, 30 lines at most)
    "card_summary_requested_ascii": {"fr": "Requested card: {title} -- '{question}'"},
    "card_summary_requested": {"fr": "Requested card: {title} — '{question}'"},
    "card_summary_alternatives": {"fr": "Alternatives disponibles : {alternatives}."},
    "card_selection_verb_explicit": {"fr": "Requested card"},
    "card_selection_verb_suggested": {"fr": "Suggested card"},
    "card_selection_verb_fallback": {"fr": "Default card"},
    "plan_unmapped_actuals_note": {"fr": "Unmapped actuals: {note}"},
    "plan_unmapped_actuals_none": {"fr": "Unmapped actuals: none"},
    "plan_unmapped_out_of_window": {
        "fr": " (including outside the mapped rows window)"
    },
    "plan_unmapped_actuals_count": {
        "fr": "Unmapped actuals: {count} campaign(s), {spend} €{suffix}"
    },
    "plan_pacing_plan_line": {"fr": "Plan : {plan}  |  Version active : {version}"},
    "plan_pacing_as_of": {"fr": "As of: {day}"},
    "plan_pacing_plan_only_suffix": {"fr": " [plan seul]"},
}


# ---------------------------------------------------------------------------
# Metric names. Source-agnostic (AD-2): these are WAREHOUSE-vocabulary metric
# names, not module names. An unknown metric keeps its own key -- inventing a
# label for a measure nobody declared would name something that does not exist.
# ---------------------------------------------------------------------------

METRIC_LABELS: dict[str, dict[str, str]] = {
    "fr": {
        "clicks": "Clics",
        "impressions": "Impressions",
        "sessions": "Sessions",
        "active_users": "Utilisateurs actifs",
        "conversions": "Conversions",
        "cost": "Cost",
        "revenue": "Revenu",
        "average_position": "Position moyenne",
        "roas": "ROAS",
        "ctr": "CTR",
        "cpa": "CPA",
    },
}


# ---------------------------------------------------------------------------
# The word a sentence prints for a canonical dimension WHEN THE CLIENT HAS NAMED
# NONE. This is the `default` argument of `narrative.dimension_word` (story
# 27.9), and its contract is unchanged: the client's own word wins, the canonical
# identifier NEVER reaches a sentence, and the fallback is the prose the card
# ships. Only the spelling of that fallback moved here.
# ---------------------------------------------------------------------------

DIMENSION_DEFAULT_WORDS: dict[str, dict[str, str]] = {
    "fr": {
        "query": "Requête",
        "user_type": "Type",
        "device_category": "Segment",
        "country": "pays",
        "landing_page": "Page d'entrée",
        "session_source_medium": "Canal",
    },
}


class UnknownPhraseKey(KeyError):
    """A key no catalogue entry spells.

    A NAMED FAILURE, not a silent fallback to the key itself: a sentence that
    printed `keywords_top_mover` to a reader would be a defect nobody could see
    in a test that only checks a line was produced.
    """


def _language(language: str | None) -> str:
    if language and language in SUPPORTED_NARRATIVE_LANGUAGES:
        return language
    return DEFAULT_NARRATIVE_LANGUAGE


def phrase(key: str, /, *, language: str | None = None, **fields: object) -> str:
    """Render the catalogue entry *key* in the reader's language (PURE).

    *language* is the render parameter the arbitration of 2026-08-22 requires;
    when it is absent -- which is every caller today, because no request carries
    one -- ``DEFAULT_NARRATIVE_LANGUAGE`` applies. An unsupported language falls
    back to that default rather than to the key: a reader gets a sentence they
    can read in some language, never an identifier.
    """
    try:
        templates = PHRASES[key]
    except KeyError as exc:  # pragma: no cover -- guarded by test_narrative_phrases
        raise UnknownPhraseKey(key) from exc
    template = templates.get(_language(language)) or templates[DEFAULT_NARRATIVE_LANGUAGE]
    return template.format(**fields) if fields else template


def metric_label(metric: str, *, language: str | None = None) -> str:
    """The name a sentence prints for a warehouse metric (PURE, AD-2)."""
    return METRIC_LABELS.get(_language(language), {}).get(metric, metric)


def dimension_default(name: str, *, language: str | None = None) -> str:
    """The shipped word for a canonical dimension nobody has renamed (PURE).

    Falls back to the dimension's own name only when this catalogue ships no word
    for it -- which is a gap to fill here, not at a call site.
    """
    return DIMENSION_DEFAULT_WORDS.get(_language(language), {}).get(name, name)
