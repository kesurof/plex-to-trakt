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

### Installation rapide depuis un terminal

Si le dépôt est public, le script peut être téléchargé directement sur le serveur :

```bash
mkdir -p ~/plex-to-trakt && cd ~/plex-to-trakt
curl -fsSL https://raw.githubusercontent.com/kesurof/plex-to-trakt/main/plex-to-trakt.py -o plex-to-trakt.py
./plex-to-trakt.py
```

Avec `wget` à la place de `curl` :

```bash
mkdir -p ~/plex-to-trakt && cd ~/plex-to-trakt
wget -q https://raw.githubusercontent.com/kesurof/plex-to-trakt/main/plex-to-trakt.py
./plex-to-trakt.py
```

### Depuis Git

Si Git est déjà configuré sur le serveur :

```bash
git clone https://github.com/kesurof/plex-to-trakt.git
cd plex-to-trakt
./plex-to-trakt.py
```

Pour un dépôt privé avec une clé SSH GitHub configurée :

```bash
git clone git@github.com:kesurof/plex-to-trakt.git
cd plex-to-trakt
./plex-to-trakt.py
```

### Lancements suivants

Une fois installé, il suffit de revenir dans le dossier et de lancer le script :

```bash
cd ~/plex-to-trakt
./plex-to-trakt.py
```

Il est également possible de le lancer directement avec Python :

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
