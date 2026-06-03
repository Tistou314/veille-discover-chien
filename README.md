# Veille Discover — niche chien (chien.pagesjaunes.fr)

Petit agrégateur qui, chaque matin, récupère les flux RSS des sites chien qui font
du Discover, regroupe les sujets, score leur potentiel Discover et te sort un
tableau de bord HTML triable où tu n'as plus qu'à piocher **un** sujet.

## Installation
```bash
pip install feedparser
```

## Usage quotidien
```bash
python3 veille_discover_chien.py
```
Ouvre ensuite `digest/digest_AAAA-MM-JJ.html` dans ton navigateur.

Options :
- `--jours N` : fenêtre de fraîcheur (défaut 4 jours)
- `--no-history` : ignore l'historique (tout est considéré comme neuf)
- `--demo` : utilise les flux locaux de `demo_flux/` (aucun réseau, pour tester)

## Déploiement

### ⭐ Le plus simple (recommandé) — GitHub Actions + Pages, zéro serveur
Cron natif, mémoire "déjà vu" qui persiste, page consultable au téléphone, gratuit,
rien à administrer. Le workflow est déjà fourni dans `.github/workflows/veille.yml`.

1. Crée un dépôt GitHub (privé ou public) et pousse-y le contenu de ce dossier
   (le `.py`, `sources_chien.json`, et le dossier `.github/`).
2. Settings → Pages → Source : **Deploy from a branch** → branche `main`, dossier
   `/docs` → Save.
3. Onglet Actions → autorise les workflows. Clique **Run workflow** une première
   fois pour générer le premier digest (ou attends 7h).
4. Ton digest est en ligne : `https://<ton-user>.github.io/<ton-repo>/` — mets-le
   en favori sur ton téléphone. C'est toujours le dernier (via `index.html`).

Le robot tourne chaque matin (cron `0 5 * * *` = ~7h Paris l'été), regénère la page
et recommit `historique.json` pour ne pas te re-proposer les mêmes sujets.

**Confidentialité** : en dépôt public, la page est accessible à qui connaît l'URL.
Le contenu n'est que des liens vers des articles déjà publics + des scores, donc le
risque est faible. Pour du vraiment privé, Pages sur dépôt privé demande un plan
payant ; dis-le moi, on a des alternatives.

### Option B — En local (pour tester vite)
Sur ta machine, via `crontab -e` :
```cron
0 7 * * *  cd /chemin/vers/veille && /usr/bin/python3 veille_discover_chien.py >> veille.log 2>&1
```
Tu ouvres `digest/index.html`. Limite : la machine doit être allumée à 7h.

### Option C — o2switch (seulement si tu y tiens)
Faisable mais plus de friction quand on vient de WordPress : cPanel → "Setup Python
App", `pip install feedparser`, puis un Cron Job pointant
`python veille_discover_chien.py --sortie ~/public_html/veille`, avec un `.htpasswd`
sur le dossier. Le chemin du virtualenv `activate` est celui affiché par cPanel,
ne le devine pas.

### Sortie configurable
`--sortie DOSSIER` change le dossier de sortie (défaut : `./digest`). Le script
écrit `digest_AAAA-MM-JJ.html` (archive) **et** `index.html` (toujours le dernier).

## Vérifier l'état des sources (à faire une fois avant de planifier)
```bash
python3 veille_discover_chien.py --check
```
Teste chaque source et rapporte, sans rien publier : `OK [rss]` ou `OK [sitemap]`
avec le nombre d'items et l'URL résolue, ou `INTROUVABLE`. Tu vois donc d'un coup
d'œil lesquelles répondent. Pour une source `INTROUVABLE`, trouve son flux et
colle-le dans le champ `flux` de `sources_chien.json`, ou mets `actif: false`.
Une source qui échoue est simplement ignorée : elle ne casse jamais le digest.

## Comment ça marche
1. **Résolution de chaque source** (dans l'ordre) : flux fourni → auto-découverte
   du flux (balise `<link>` puis `/feed/`, `/rss`…) → **repli sur le sitemap news**
   (`/sitemap-news.xml` etc.) pour les sites Discover qui n'exposent pas de RSS ; le
   titre est alors dérivé du slug d'URL (souvent = le titre Discover). Une source
   sans rien d'exploitable est ignorée proprement (jamais d'item cassé au digest).
2. **Filtrage chien** : ne garde que les articles concernant le chien (mots-clés
   canins + sujets transversaux comme canicule/voiture/refuge). Le pur "chat" est
   écarté.
3. **Dédup URL** : un même article servi sur deux rubriques d'un site est fusionné.
4. **Regroupement de sujets** : un même fait divers reformulé par plusieurs sources
   est regroupé → badge **chaud** et bonus de score (sujet repris = fort potentiel
   Discover).
5. **Scoring Discover** : signaux de titre (émotion, sauvetage, chiffre, question,
   tournure récit…) + fraîcheur + priorité de la source + bonus multi-sources.
6. **Historique** : `historique.json` mémorise ce qui a déjà été affiché ; les jours
   suivants ces sujets sont dégradés pour laisser remonter le frais.

## Éditer les sources
Tout est dans `sources_chien.json`. Pour chaque source :
- `nom`, `home` (obligatoires)
- `flux` : laisse vide pour l'auto-découverte, ou colle l'URL du flux
- `priorite` : 1 à 3 (3 = source très Discover, pèse plus dans le score)
- `actif` : `false` pour la désactiver sans la supprimer

## Élargir la liste des sources
- **Méthode mobile (la plus fiable)** : sur un Android (ou l'app Google iOS), suis
  les centres d'intérêt liés au chien et observe pendant quelques jours quelles
  sources reviennent dans Discover. Ajoute-les ici.
- **Google News** : recherche des sujets chien sur news.google.com ; les sources qui
  remontent sont candidates (elles sont quasi toujours aussi en Discover).
- **Annuaires RSS animaux** : `atlasflux.saynete.net` (rubrique Animaux, avec
  fréquence de parution par flux) et `facteur-info.com` listent des flux vérifiés.
- **Agrégats thématiques prêts à l'emploi** : Flipboard expose des flux par thème,
  déjà inclus ici — `https://flipboard.com/topic/fr-chien.rss` et
  `https://flipboard.com/topic/fr-animaux.rss` (ce sont des agrégats de ce qui
  circule sur le sujet, utiles pour repérer un sujet chaud transverse).

## Réglages fins (dans le script)
- `MOTS_DISCOVER` : le dictionnaire de signaux de titre et leur poids — enrichis-le
  avec ton vocabulaire Discover maison.
- `regrouper_sujets(seuil=0.42, communs_min=3)` : sensibilité du regroupement de
  sujets. Baisse `communs_min` pour regrouper plus agressivement (au risque de
  fusionner des sujets différents).

## Limites assumées
- Le regroupement attrape les reprises proches ; deux reformulations très
  divergentes peuvent rester séparées (le tri visuel du matin tranche).
- Le filtrage chien/chat est heuristique sur mots-clés.
- `historique.json` grossit avec le temps ; purge-le si besoin (le supprimer
  remet tout à neuf).
