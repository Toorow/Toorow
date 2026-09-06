"""Les reparations d'acces du rapport d'audit 12 -- contre un VRAI Postgres.

Chantier 67-17, 2026-08-17. Trois classes de defaut, trois preuves, et chacune
demande une base : la resolution super-admin lit `app.person_identities`, le
pin d'une sous-ressource est une clause `WHERE`, et la revocabilite d'une
invitation depend d'un projet qui n'existe plus. Un faux de connexion ne peut
prouver aucune des trois -- il rendrait exactement les lignes qu'on lui donne,
donc il rendrait la reponse qu'on veut lire.

Gate sur `TEST_POSTGRES_DSN` / `PLATFORM_DB_URL` comme toutes les suites vivantes ;
chaque test nettoie ce qu'il ecrit.
"""

from __future__ import annotations

import os

import pytest
from ulid import ULID

_DSN = os.environ.get("TEST_POSTGRES_DSN") or os.environ.get("PLATFORM_DB_URL", "")


def _pg_reachable() -> bool:
    if not _DSN:
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(_DSN, connect_timeout=2) as conn:
            return conn is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="TEST_POSTGRES_DSN / PLATFORM_DB_URL not reachable -- skipping live access tests",
)

_ISSUER = "https://test.invalid/access-hardening-67-17"
_SUPER_ADMIN_EMAIL = "boss@example.com"


@pytest.fixture
def conn():
    import psycopg  # noqa: PLC0415

    with psycopg.connect(_DSN, connect_timeout=5) as connection:
        yield connection
        connection.rollback()
        _scrub(connection)
        connection.commit()


#: Le nettoyage est DERIVE, pas ecrit a la main -- mais il est derive par le
#: GRAPHE, pas par colonne.
#:
#: CE QUE LA VERSION PAR COLONNE LAISSAIT DERRIERE ELLE (AI-377, mesure le
#: 2026-09-06 sur la base jetable). Elle balayait `DELETE FROM app.<table> WHERE
#: org_id LIKE 'org_67_17_%%'` pour toute table portant `org_id`, chaque echec
#: etant avale par `ROLLBACK TO SAVEPOINT`. Or `app.mdm_business_domain_versions`
#: est append-only (`trg_mdm_business_domain_versions_immutable`, migration 098)
#: et REFUSE le DELETE tant que `app.rgpd_erasure` n'est pas leve, tandis que
#: `app.business_domain_version_registry` (migration 330), lui, l'accepte. Chaque
#: org de fixture partait donc avec ses 6 versions de domaine semees par
#: `trg_organizations_seed_business_domains` encore en base et leurs 6 lignes de
#: registre effacees -- 24 orgs, 144 paires -- et `app.organizations` restait
#: elle aussi (les versions la retiennent en RESTRICT). L'invariant que la
#: migration 330 affirme dans son propre bloc `DO` (registre == union des deux
#: registres) etait alors FAUX pour toute la base, ce qui rougissait
#: `test_a_pin_the_validator_accepts_is_stored_pg::
#: test_the_registry_is_exactly_the_union_the_validators_judge` -- un test
#: etranger, pour une raison qui n'etait pas la sienne.
#:
#: CE QU'ON FAIT A LA PLACE : le chemin produit, `tests.conftest.purge_fixture_org`
#: -> `core.org_purge.purge_org_tree`, qui parcourt le graphe de cles etrangeres
#: et pose `SET LOCAL app.rgpd_erasure = 'on'` -- l'echappatoire que les ledgers
#: append-only exigent. Une table ajoutee demain est demontee sans que personne y
#: pense, et rien n'est avale : un refus remonte. Les orgs sont ENUMEREES par
#: prefixe, donc un run precedent interrompu est nettoye lui aussi.
def _scrub(connection) -> None:
    from tests.conftest import purge_fixture_org  # noqa: PLC0415 -- import paresseux

    with connection.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.organizations WHERE id LIKE %s", ("org_67_17_%",)
        )
        org_ids = [row[0] for row in cur.fetchall()]

    for org_id in org_ids:
        purge_fixture_org(connection, org_id)

    with connection.cursor() as cur:
        cur.execute("DELETE FROM app.person_identities WHERE issuer = %s", (_ISSUER,))
        cur.execute("DELETE FROM app.persons WHERE id LIKE %s", ("person_67_17_%",))
        cur.execute(
            "DELETE FROM app.user_profiles WHERE identity LIKE %s", ("person_67_17_%",)
        )


def _person(conn, suffix: str, *, verified_email: str | None) -> str:
    person_id = f"person_67_17_{suffix}"
    with conn.cursor() as cur:
        # Idempotent : un run precedent interrompu avant son nettoyage ne doit pas
        # rendre celui-ci rouge pour une raison qui n'est pas la sienne.
        cur.execute(
            "DELETE FROM app.person_identities WHERE issuer = %s AND subject = %s",
            (_ISSUER, f"sub-{suffix}"),
        )
        cur.execute(
            "INSERT INTO app.persons (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (person_id,),
        )
        cur.execute(
            "INSERT INTO app.person_identities "
            "(id, person_id, issuer, subject, verified_email, verified_email_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW())",
            (f"pid_{ULID()}", person_id, _ISSUER, f"sub-{suffix}", verified_email),
        )
    conn.commit()
    return person_id


# --------------------------------------------------------------------------
# CLASSE 1 -- une seule resolution serveur de << qui est super-admin >>.
# --------------------------------------------------------------------------


def test_the_canonical_person_resolves_through_its_verified_email(conn, monkeypatch):
    """La comparaison brute etait MORTE ; la resolution la ranime.

    C'est la mesure du defaut, pas seulement de la reparation : `is_super_admin`
    recoit ce que `authenticate_api_request` rend en mode canonique -- un
    `person_<ULID>` -- et le compare a une liste d'e-mails. Elle ne peut pas
    repondre vrai. Le POST /api/admin/org-plan humain, les trois lectures
    connecteur de `operations_mcp` et les cinq outils d'horloge etaient tous
    gardes par cette comparaison-la.
    """
    from core.super_admin import identity_is_super_admin, is_super_admin

    monkeypatch.setenv("TOOROW_SUPER_ADMINS", _SUPER_ADMIN_EMAIL)
    person_id = _person(conn, "boss", verified_email=_SUPER_ADMIN_EMAIL)

    assert is_super_admin(person_id) is False, (
        "le defaut a disparu du test avant d'avoir disparu du code : la liste est "
        "indexee par e-mail, un person_id ne peut jamais y figurer"
    )
    assert identity_is_super_admin(person_id, conn=conn) is True


def test_a_self_declared_profile_email_grants_nothing(conn, monkeypatch):
    """Un signal d'autorite ne lit jamais un champ que son sujet ecrit.

    `me_api` calculait `is_super_admin` sur `user_profiles.email`, que la personne
    PATCHe elle-meme. La porte tenait -- le signal, non.
    """
    from core.super_admin import identity_is_super_admin

    monkeypatch.setenv("TOOROW_SUPER_ADMINS", _SUPER_ADMIN_EMAIL)
    person_id = _person(conn, "impostor", verified_email="someone.else@example.com")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.user_profiles (identity, email) VALUES (%s, %s) "
            "ON CONFLICT (identity) DO UPDATE SET email = EXCLUDED.email",
            (person_id, _SUPER_ADMIN_EMAIL),
        )
    conn.commit()

    assert identity_is_super_admin(person_id, conn=conn) is False


def test_a_legacy_email_identity_still_resolves_without_a_registry_row(conn, monkeypatch):
    """La double cle survit a la reparation : une identite heritee EST un e-mail."""
    from core.super_admin import identity_is_super_admin

    monkeypatch.setenv("TOOROW_SUPER_ADMINS", _SUPER_ADMIN_EMAIL)
    assert identity_is_super_admin(_SUPER_ADMIN_EMAIL, conn=conn) is True


def test_an_empty_allow_list_admits_nobody(conn, monkeypatch):
    """Deny-by-default, resolution comprise."""
    from core.super_admin import identity_is_super_admin

    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "")
    person_id = _person(conn, "nobody", verified_email=_SUPER_ADMIN_EMAIL)
    assert identity_is_super_admin(person_id, conn=conn) is False
    assert identity_is_super_admin(None, conn=conn) is False


# --------------------------------------------------------------------------
# CLASSE 2 -- une sous-ressource d'URL est epinglee a son parent d'URL.
# --------------------------------------------------------------------------


def _org_with_two_projects(conn, actor: str) -> tuple[str, str, str]:
    org_id = f"org_67_17_{ULID()}"
    project_a = f"proj_67_17_a_{ULID()}"
    project_b = f"proj_67_17_b_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (org_id, "Audit 12", f"audit12{ULID()}".lower()[:40], actor),
        )
        for project_id, name in ((project_a, "Alpha"), (project_b, "Beta")):
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (project_id, name, f"{name.lower()}{ULID()}".lower()[:40], actor, org_id),
            )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status) "
            "VALUES (%s, %s, %s, 'owner', 'active')",
            (f"orgm_{ULID()}", org_id, actor),
        )
    conn.commit()
    return org_id, project_a, project_b


def test_a_grant_change_of_another_project_is_not_found(conn):
    """L'autorite prouvee sur un projet ne se depense pas sur un autre.

    Le confirm autorisait `manage` sur le `project_id` de l'URL puis chargeait le
    changement par son seul `change_id`. Ici l'acteur a bien manage sur les DEUX
    projets -- ce n'est donc pas le refus d'un intrus qu'on mesure, c'est le lien
    entre l'URL et la sous-ressource, qui doit tenir meme quand l'appelant aurait
    pu obtenir la meme chose par la bonne adresse.
    """
    from core.project_access_surface import (
        ProjectAccessUnavailable,
        confirm_grant_change,
        issue_grant_confirmation,
        prepare_grant_change,
    )

    actor = "owner-67-17@example.com"
    _org, project_a, project_b = _org_with_two_projects(conn, actor)

    prepared = prepare_grant_change(
        conn,
        project_id=project_b,
        identity=actor,
        after_capability="view",
        actor=actor,
        idempotency_key=f"idem-{ULID()}",
    )
    conn.commit()
    change_id = prepared["change_id"]

    # L'ADRESSE MENTEUSE : le change appartient a Beta, l'URL nomme Alpha.
    with pytest.raises(ProjectAccessUnavailable):
        issue_grant_confirmation(
            conn, change_id=change_id, actor=actor, project_id=project_a
        )
    conn.rollback()
    with pytest.raises(ProjectAccessUnavailable):
        confirm_grant_change(
            conn,
            change_id=change_id,
            actor=actor,
            confirmation_id="whatever",
            confirmation_secret="whatever",
            project_id=project_a,
        )
    conn.rollback()

    # Rien n'a ete accorde, et le changement est reste `prepared`.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.project_grant_changes WHERE id = %s", (change_id,)
        )
        assert cur.fetchone()[0] == "prepared"
        cur.execute(
            "SELECT count(*) FROM app.resource_grants WHERE scope_id IN (%s, %s)",
            (project_a, project_b),
        )
        assert cur.fetchone()[0] == 0


def test_the_pin_selects_the_row_it_should_and_refuses_further_on(conn):
    """Le pin n'est pas un refus universel : la BONNE adresse passe la porte.

    Avec le bon `project_id` on ne recoit plus `ProjectAccessUnavailable` -- la
    ligne a ete trouvee -- mais l'echec de la confirmation, qui vit apres. Sans
    ce controle, un `WHERE` casse (mauvais parametre, mauvais ordre) rendrait le
    test precedent vert pour la mauvaise raison.
    """
    from core.project_access_surface import (
        ProjectAccessUnavailable,
        confirm_grant_change,
        prepare_grant_change,
    )

    actor = "owner-67-17b@example.com"
    _org, _project_a, project_b = _org_with_two_projects(conn, actor)
    prepared = prepare_grant_change(
        conn,
        project_id=project_b,
        identity=actor,
        after_capability="view",
        actor=actor,
        idempotency_key=f"idem-{ULID()}",
    )
    conn.commit()

    with pytest.raises(Exception) as caught:
        confirm_grant_change(
            conn,
            change_id=prepared["change_id"],
            actor=actor,
            confirmation_id="nope",
            confirmation_secret="nope",
            project_id=project_b,
        )
    conn.rollback()
    assert not isinstance(caught.value, ProjectAccessUnavailable), (
        "la bonne adresse doit trouver la ligne ; l'echec doit venir de la "
        "confirmation, pas du chargement"
    )


# --------------------------------------------------------------------------
# CLASSE 4 -- la revocation retire, elle ne valide pas.
# --------------------------------------------------------------------------


def _hash64(seed: str) -> str:
    """Les colonnes de hachage sont contraintes `^[0-9a-f]{64}$` -- on la respecte."""
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(seed.encode()).hexdigest()


def _invitation_bound_to_a_dead_scope(conn, org_id: str, issuer: str) -> str:
    invitation_id = f"invite_67_17_{ULID()}"
    dead_project = f"proj_67_17_gone_{ULID()}"
    operation_id = f"op_67_17_{ULID()}"
    with conn.cursor() as cur:
        # `invitations.operation_id` porte une cle etrangere : l'invitation n'existe
        # pas sans l'operation qui l'a emise. On pose la ligne minimale valide.
        cur.execute(
            "INSERT INTO app.operations "
            "(id, effective_org_id, command_type, actor, resource_path, host_context, "
            " versions, request_hash, confirmation_mode, idempotency_key_hash, state) "
            "VALUES (%s, %s, 'invitation.issue', %s, %s, '{}'::jsonb, '{}'::jsonb, %s, "
            " 'none', %s, 'succeeded')",
            (
                operation_id,
                org_id,
                issuer,
                f'["organization:{org_id}"]',
                _hash64(f"request-{invitation_id}"),
                _hash64(f"idem-{invitation_id}"),
            ),
        )
        cur.execute(
            "INSERT INTO app.invitations "
            "(id, invited_identity_hash, org_id, invited_role, grant_bindings, issuer, "
            " policy_version, bearer_hash, expires_at, operation_id) "
            "VALUES (%s, %s, %s, 'member', %s, %s, 'v1', %s, NOW() + interval '2 days', %s)",
            (
                invitation_id,
                _hash64(f"invited-{invitation_id}"),
                org_id,
                # Le scope granté ne resout plus : le projet n'existe pas (archive,
                # supprime, ou jamais cree). C'est exactement l'etat qui rendait
                # l'invitation irrevocable.
                f'[{{"scope_type": "project", "scope_id": "{dead_project}", '
                f'"capability": "view"}}]',
                issuer,
                _hash64(f"bearer-{invitation_id}"),
                operation_id,
            ),
        )
    conn.commit()
    return invitation_id


def test_a_dead_granted_scope_no_longer_makes_an_invitation_irrevocable(conn):
    """Elle retire ; elle n'accorde rien -- donc elle ne prouve rien sur les scopes.

    Un projet archive ou supprime rendait l'invitation qui le grantait
    IRREVOCABLE par son propre manager d'org : un grant en attente que personne
    ne pouvait retirer. La revocation demande desormais manage sur l'ORGANISATION,
    et que l'invitation vive bien dans cette organisation-la.
    """
    from core.invitations_api import _authorize_invitation_binding

    actor = "manager-67-17@example.com"
    org_id, _a, _b = _org_with_two_projects(conn, actor)
    invitation_id = _invitation_bound_to_a_dead_scope(conn, org_id, actor)

    revoke = _authorize_invitation_binding(
        conn,
        identity=actor,
        org_id=org_id,
        invitation_id=invitation_id,
        require_bindings=False,
    )
    assert revoke is None, "la revocation doit passer malgre le scope mort"


def test_resending_still_proves_authority_over_every_granted_scope(conn):
    """Le renvoi RE-DELIVRE les grants : lui doit encore les prouver.

    La distinction est le fond de la reparation. Si les deux actes cedaient, la
    correction aurait ouvert une porte au lieu d'en debloquer une.
    """
    from core.invitations_api import _authorize_invitation_binding

    actor = "manager-67-17b@example.com"
    org_id, _a, _b = _org_with_two_projects(conn, actor)
    invitation_id = _invitation_bound_to_a_dead_scope(conn, org_id, actor)

    resend = _authorize_invitation_binding(
        conn,
        identity=actor,
        org_id=org_id,
        invitation_id=invitation_id,
        require_bindings=True,
    )
    assert resend is not None and resend.status_code == 404, (
        "le renvoi doit toujours refuser quand un scope granté ne se resout plus"
    )


def test_an_invitation_of_another_organization_is_not_found(conn):
    """Le pin de la classe 2, cote invitations : il tenait deja, il doit tenir."""
    from core.invitations_api import _authorize_invitation_binding

    actor = "manager-67-17c@example.com"
    org_id, _a, _b = _org_with_two_projects(conn, actor)
    other_org, _c, _d = _org_with_two_projects(conn, actor)
    invitation_id = _invitation_bound_to_a_dead_scope(conn, other_org, actor)

    denied = _authorize_invitation_binding(
        conn,
        identity=actor,
        org_id=org_id,
        invitation_id=invitation_id,
        require_bindings=False,
    )
    assert denied is not None and denied.status_code == 404
