# Vérifications hors ligne

Trois scripts, **aucun appel API, aucune clé nécessaire**. À lancer depuis la
racine du dépôt (ils lisent `styles.css`, les prompts et les schémas) :

```bash
.venv/bin/python tests/check_store.py && .venv/bin/python tests/check_concurrence.py && .venv/bin/python tests/check_integration.py
```

| Script | Ce qu'il verrouille |
| --- | --- |
| `check_store.py` | Schéma SQLite, cache OCR gzippé, index partiel « un seul run en cours », idempotence de `upsert_fiche`, cumul incrémental des jetons, suppression en cascade, aller-retour d'archive, et le refus des archives hostiles (traversée de chemin, fichier `.sqlite`, version future, bombe de taille). |
| `check_concurrence.py` | Échauffement du cache de prompt **avant** le fan-out, plafond de concurrence, ordre document rétabli malgré un achèvement désordonné, un 429 isolé n'emporte pas le run, et un bug de notre code avorte à l'échauffement au lieu d'être payé N fois. |
| `check_integration.py` | Le chemin complet avec un faux client : OCR → découpage → extraction parallèle → persistance → rechargement → Excel, plus le cache OCR au 2ᵉ passage, la relance des fiches en échec, le mode par lot (soumission et récolte, y compris les segments sans résultat) et l'échappement HTML. |

Ils utilisent `assert` et non `pytest` : le dépôt n'a pas encore de suite de
tests, et la tâche 3 doit décider de la forme définitive (fixtures OCR figées,
harnais d'évaluation). Ces scripts sont conçus pour être repris tels quels comme
premiers cas de cette suite.

`check_integration.py` construit sa fiche de test **depuis `REX.schema.json`**
plutôt qu'en dur, pour ne pas se désynchroniser au premier ajout de champ.
