# Plex → Trakt Exporter

Petit outil standalone en Python pour exporter l'historique de visionnage Plex vers un fichier JSON compatible avec l'import Trakt.

Le script :

- détecte les utilisateurs Plex
- récupère l'historique de visionnage
- utilise les identifiants Plex quand ils sont disponibles
- peut compléter les correspondances avec TMDB et TVDB
- conserve uniquement les correspondances fiables
- supprime les doublons en gardant le visionnage le plus récent
- génère un fichier `trakt.json` prêt à importer

Aucune donnée n'est envoyée automatiquement vers Trakt.

## Installation

### Prérequis

- Python 3
- Plex Media Server
- une clé API TVDB
- un token ou une clé API TMDB

Aucune dépendance Python externe n'est nécessaire.

### Installation

Télécharger le script :

```bash
plex-to-trakt.py
```

Puis le rendre exécutable :

```bash
chmod +x plex-to-trakt.py
```

Lancer :

```bash
./plex-to-trakt.py
```

Ou :

```bash
python3 plex-to-trakt.py
```

## Utilisation

Au lancement, un menu est affiché :

```text
1. Exporter un historique Plex
2. Configurer les API
3. Configurer le chemin Plex
4. Tester la configuration
5. Quitter
```

### Première configuration

Configurer :

- le chemin vers la base SQLite de Plex
- le token ou la clé API TMDB
- la clé API TVDB
- éventuellement le PIN TVDB

La configuration est enregistrée dans :

```text
config.json
```

dans le même dossier que le script.

### Exporter un historique

Choisir :

```text
1. Exporter un historique Plex
```

Le script affiche les utilisateurs Plex trouvés ainsi que leur nombre de lectures.

Sélectionner l'utilisateur à exporter.

Avant l'export, le script vérifie automatiquement :

- la base Plex
- l'accès TMDB
- l'accès TVDB
- le dossier d'export

## Fichiers générés

Les exports sont créés dans :

```text
exports/
```

Exemple :

```text
exports/
└── utilisateur-20260828-170000/
    ├── trakt.json
    ├── review.json
    ├── resolved-api.json
    └── report.txt
```

### trakt.json

Fichier final à importer dans Trakt.

Les doublons sont supprimés et seul le visionnage le plus récent d'un même média est conservé.

### review.json

Contient les films ou épisodes qui n'ont pas pu être identifiés avec suffisamment de certitude.

### resolved-api.json

Détail des éléments identifiés grâce à TMDB ou TVDB.

### report.txt

Résumé de l'export :

- nombre de lectures Plex
- éléments identifiés
- éléments résolus par API
- éléments à vérifier
- doublons supprimés
- nombre final d'éléments uniques

## Import dans Trakt

Importer ensuite le fichier :

```text
trakt.json
```

dans l'outil d'import Trakt.

Pour les épisodes, il est recommandé de conserver la correspondance par identifiant TVDB proposée par défaut par Trakt.
