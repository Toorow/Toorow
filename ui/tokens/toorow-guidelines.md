# toorow — règles du design system

Produit : plateforme analytics marketing (cartes de réponse, datastreams, console).
Style : éditorial, doux, précis.

## Typographie
- **Lexend** — titres et logo uniquement. Poids 500–600, jamais 700+. h1 44px −0.02em.
- **Plus Jakarta Sans** — corps de texte et tableaux. 15px / 1.6.
- **JetBrains Mono** — logs MCP et code dbt exclusivement. Jamais pour du texte courant.
- Chiffres tabulaires (`tabular-nums`) sur **toute** donnée numérique : le chiffre est le héros.

## Couleur
- **Un seul accent interactif** : rose `#FF99C8` (hover `#F77FB4`), texte `#111111` dessus.
- `#FBB2CC` et `#F7CAD0` : fonds d'accent doux et surfaces teintées — pas d'interaction.
- **Lavande `#D1C4E9` : décoratif uniquement** (dégradés, illustrations). Jamais cliquable.
- Fond de page `#F8F9FA`, surfaces blanches, texte `#111111` (jamais #000 pur).
- Sémantiques : erreur `#D64550`/`#FFD6DB`, warning `#E8A13D`/`#FCE8CD`,
  succès `#3E9B6E`/`#CDEBDD`, info `#7E6BC4`/`#D1C4E9` — la teinte pleine pour texte/icône,
  le conteneur pastel pour les fonds de badge/bannière.
- Dataviz : dérouler la palette catégorielle dans l'ordre (rose, lavande profonde, menthe,
  abricot, bleu doux, corail) ; track neutre `#EBEBF3`.

## Forme & profondeur
- Boutons et chips : **pill** (radius 999). Cartes : radius 16. Champs : radius 12.
- Surfaces **plates** : séparation par contraste de fond (`#F8F9FA` vs blanc),
  ombre de carte minimale — jamais d'ombres portées lourdes.
- Espacement généreux : padding carte 24px, sections 32–48px, lignes de tableau 52px.

## Ton
- Interface en **français** (accents compris). Le contenu d'exemple est réaliste
  (canaux Organique/Payant/Direct/Social/Référent, métriques sessions/conversions/CPA).
- Pas de look « admin générique » : pas de bleu par défaut, pas de boutons en relief.
