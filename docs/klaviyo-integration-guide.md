# Guide d'Intégration & Templates Emails Klaviyo — toorow

Ce document détaille l'intégration des **Flows et Templates d'Emails Klaviyo** pour le cycle de vie des utilisateurs de **toorow**. 

Tous les templates respectent strictement les règles du **Design System toorow** (`ui/tokens/toorow-guidelines.md`) et intègrent un guidage technique adapté à l'étape exacte de l'utilisateur (avec références vers la documentation officielle).

---

## 1. Principes du Design System dans les Emails

- **Style Général** : Éditorial, doux, précis.
- **Palette de Couleurs** :
  - **Accent CTA principal** : Rose `#FF99C8` (hover `#F77FB4`), texte `#111111`.
  - **Arrière-plan page** : Gris neutre `#F8F9FA`.
  - **Cartes & Conteneurs** : Blanc pur `#FFFFFF`, rayon `16px`, bordure subtile `#EBEBF3`.
  - **Teintes & Décorations** : Lavande `#D1C4E9`, Rose doux `#FBB2CC`.
  - **Badges Sémantiques** :
    - Succès : Texte `#3E9B6E`, Conteneur `#CDEBDD`
    - Warning : Texte `#E8A13D`, Conteneur `#FCE8CD`
    - Info / Étape : Texte `#7E6BC4`, Conteneur `#D1C4E9`
- **Typographies** :
  - Titres & Marque : **Lexend** (Google Fonts).
  - Corps de texte : **Plus Jakarta Sans** (Google Fonts).
  - Métriques & Snippets Code : **JetBrains Mono** (Google Fonts).
- **Formes** : Boutons et puces en format **Pill** (`border-radius: 999px`).

---

## 2. Cartographie des 7 Événements & Templates HTML

Les templates sont stockés dans le dossier [`docs/klaviyo-templates/`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/).

| Étape / Event Metric | Déclencheur Backend | Fichier Template HTML | Lien de Documentation Associé |
| :--- | :--- | :--- | :--- |
| **`waitlisted`** | Soumission du formulaire de liste d'attente sur toorow. | [`01-waitlisted.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/01-waitlisted.html) | [`docs/introduction.mdx`](file:///c:/Users/littl/Programmation/connector/docs/introduction.mdx) |
| **`invited_to_test`** | Le super-admin invite l'utilisateur depuis la console. | [`02-invited-to-test.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/02-invited-to-test.html) | [`docs/quickstart.mdx`](file:///c:/Users/littl/Programmation/connector/docs/quickstart.mdx) |
| **`invite_expired`** | Le jeton d'invitation atteint l'expiration de 7 jours. | [`03-invite-expired.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/03-invite-expired.html) | [`docs/project-context.md`](file:///c:/Users/littl/Programmation/connector/docs/project-context.md) |
| **`signed_up`** | L'utilisateur accepte l'invitation et crée son organisation. | [`04-signed-up.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/04-signed-up.html) | [`docs/integration-architecture.md`](file:///c:/Users/littl/Programmation/connector/docs/integration-architecture.md) |
| **`first_datastream_connected`** | L'organisation connecte son 1er datastream (GA4, Ads...). | [`05-first-datastream-connected.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/05-first-datastream-connected.html) | [`docs/development-guide.md`](file:///c:/Users/littl/Programmation/connector/docs/development-guide.md) |
| **`trial_limit_reached`** | L'organisation atteint 3/3 datastreams (limite d'essai). | [`06-trial-limit-reached.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/06-trial-limit-reached.html) | [`docs/product-direction.md`](file:///c:/Users/littl/Programmation/connector/docs/product-direction.md) |
| **`upgraded`** | Le super-admin surclasse l'organisation au plan `full`. | [`07-upgraded.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/07-upgraded.html) | [`docs/adding-a-connector.md`](file:///c:/Users/littl/Programmation/connector/docs/adding-a-connector.md) |

---

## 3. Configuration des Flows dans Klaviyo

Pour chaque événement tracké par toorow via l'API REST Server-Side Klaviyo (`track_event(email, metric, properties)`), créez un **Flow Metric-Triggered** dans Klaviyo :

### Flow 1 : Waitlist Onboarding
- **Trigger** : Metric `waitlisted`
- **Action** : Envoi immédiat de l'email [`01-waitlisted.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/01-waitlisted.html).

### Flow 2 : Trial Invitation & Relance
- **Trigger** : Metric `invited_to_test`
- **Action 1** : Envoi immédiat de [`02-invited-to-test.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/02-invited-to-test.html).
- **Variables Klaviyo** : `{{ event.invite_url }}` (Contient le lien magique à usage unique).

### Flow 3 : Invitation Expirée
- **Trigger** : Metric `invite_expired`
- **Action** : Envoi de [`03-invite-expired.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/03-invite-expired.html) pour proposer un renouvellement.

### Flow 4 : Bienvenue & Activation Datastream
- **Trigger** : Metric `signed_up`
- **Action** : Envoi immédiat de [`04-signed-up.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/04-signed-up.html).
- **Time Delay (optionnel)** : Si pas de 1er datastream sous 48h, envoyer un nudge d'aide.

### Flow 5 : First Datastream Connected & MCP Setup
- **Trigger** : Metric `first_datastream_connected`
- **Action** : Envoi immédiat de [`05-first-datastream-connected.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/05-first-datastream-connected.html).
- **Variables** : `{{ event.datastreams }}` (Nom du datastream connecté).

### Flow 6 : Alerte Quota Trial (3/3 Datastreams)
- **Trigger** : Metric `trial_limit_reached`
- **Action** : Envoi de [`06-trial-limit-reached.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/06-trial-limit-reached.html).
- **Variables** : `{{ event.limit }}` (3), `{{ event.current }}` (3).

### Flow 7 : Surclassement Plan Full
- **Trigger** : Metric `upgraded`
- **Action** : Envoi immédiat de [`07-upgraded.html`](file:///c:/Users/littl/Programmation/connector/docs/klaviyo-templates/07-upgraded.html).
- **Variables** : `{{ event.plan }}` ("full").

---

## 4. Instructions d'Import dans Klaviyo

1. Créez un fichier ZIP contenant le template HTML et le dossier `assets/` en conservant les chemins relatifs. N'importez pas le HTML seul : les logos locaux ne seraient pas transférés.
2. Dans Klaviyo, naviguez vers **Content** > **Templates** > **Import**, puis importez le ZIP. Klaviyo charge les images sur son CDN et réécrit leurs références.
3. Vérifiez que les balises de désinscription `{% unsubscribe %}`, d'organisation `{{ organization.full_address }}` et de profil `{{ first_name }}` sont reconnues.
4. Configurez le sujet, le texte d'aperçu et l'adresse de réponse dans le Flow. Pour `01-waitlisted.html` :
   - **Sujet** : `You’re on the Toorow waitlist`
   - **Aperçu** : `Tell us which data connector you want to use first.`
   - **Reply-To** : `support@toorow.com`
   Pour `02-invited-to-test.html` :
   - **Sujet** : `Your Toorow invitation is ready`
   - **Aperçu** : `Accept your secure early-access invitation within 7 days.`
   - **Reply-To** : `support@toorow.com`
   Pour `03-invite-expired.html` :
   - **Sujet** : `Your Toorow invitation has expired`
   - **Aperçu** : `Request a new secure invitation when you’re ready to join.`
   - **Reply-To** : `support@toorow.com`
   Pour `04-signed-up.html` :
   - **Sujet** : `Welcome to Toorow`
   - **Aperçu** : `Your Toorow organization is ready. Connect your first marketing data source.`
   - **Reply-To** : `support@toorow.com`
   Pour `05-first-datastream-connected.html` :
   - **Sujet** : `Your first Toorow datastream is connected`
   - **Aperçu** : `Review your first connected source and prepare its first verified result.`
   - **Reply-To** : `support@toorow.com`
   Pour `06-trial-limit-reached.html` :
   - **Sujet** : `You’ve reached your Toorow trial limit`
   - **Aperçu** : `Your trial has reached its active-datastream limit.`
   - **Reply-To** : `support@toorow.com`
   Pour `07-upgraded.html` :
   - **Sujet** : `Your Toorow Full plan is active`
   - **Aperçu** : `Your organization now has Full-plan datastream and backfill entitlements.`
   - **Reply-To** : `support@toorow.com`
5. Dans **Preview & Test**, prévisualisez avec un profil qui possède un prénom et un profil sans prénom.
6. Effectuez des envois de test sur Gmail et Outlook, sur ordinateur et mobile, avec les images activées puis bloquées. Vérifiez aussi les liens, le mode sombre, l'adresse de l'organisation et la désinscription avant d'activer le Flow.
