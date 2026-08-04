# Vérifications hors ligne

**Aucun appel API, aucune clé nécessaire.** Depuis n'importe quel répertoire —
`conftest.py` se charge de se placer à la racine du dépôt :

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

| Module | Ce qu'il verrouille |
| --- | --- |
| `test_store.py` | Schéma SQLite, cache OCR gzippé, index partiel « un seul run en cours », idempotence de `upsert_fiche`, cumul incrémental des jetons, suppression en cascade, aller-retour d'archive, et le refus des archives hostiles (traversée de chemin, fichier `.sqlite`, version future, bombe de taille). |
| `test_concurrence.py` | Échauffement du cache de prompt **avant** le fan-out, plafond de concurrence, ordre document rétabli malgré un achèvement désordonné, un 429 isolé n'emporte pas le run, et un bug de notre code avorte à l'échauffement au lieu d'être payé N fois. |
| `test_integration.py` | Le chemin complet avec un faux client : OCR → découpage → extraction parallèle → persistance → rechargement → Excel, plus le cache OCR au 2ᵉ passage, la relance des fiches en échec, le mode par lot (soumission et récolte, y compris les segments sans résultat) et l'échappement HTML. |
| `test_lot.py` | Les chemins de **repli** du mode par lot (récolte par téléchargement de fichier, fichier d'erreurs illisible, JSON invalide, travail annulé) et les chemins d'échec d'un traitement (aucune fiche repérée, document déjà en cours, erreur inattendue). Base à part : le faux client renvoie toujours `batch-1`, deux soumissions dans une même base entreraient en collision. |
| `test_conformite.py` | Canonicalisation, injectivité des cinq paliers, les deux balayages de mutations, dédoublonnage, préservation des paragraphes du texte libre, précédence des verdicts, et l'absence d'exemple illégal dans le schéma. |
| `test_export.py` | Aplatissement, jointure des listes, verdict dans le classeur, unicité des noms de feuille, résolution du titre de fiche, rendu des dix sections. |
| `test_eval.py` | Le scoreur lui-même : découpage, conformité, remplissage, note composite, et le fait que le journal ne fuit aucun titre de projet. |
| `test_isolation.py` | `pipeline.py`, `store.py` et `conformite.py` n'importent pas streamlit — vérifié dans un **sous-processus**, voir plus bas. |

## Les aides partagées

| Fichier | Rôle |
| --- | --- |
| `conftest.py` | Répertoire courant, clé API factice, bases SQLite jetables, état `st.session_state` posé puis rendu. |
| `faux.py` | Le faux client Mistral, un seul pour toute la suite. Lève les **vraies** exceptions du SDK pour que `classer_erreur` soit exercée comme en production. |
| `fabrique.py` | Instances REX bâties **depuis `REX.schema.json`**, jamais écrites en dur : le schéma compte 33 champs et 12 énumérations, et une fixture figée se désynchroniserait au premier ajout de champ (tâche 5). |

## Couverture

```bash
.venv/bin/python -m coverage run --source=app,pipeline,store,conformite -m pytest
.venv/bin/python -m coverage report --precision=0
```

141 tests, ~5 s, **75 %** au total : `conformite.py` 96 %, `store.py` 91 %,
`pipeline.py` 86 %, `app.py` 60 %.

Les 60 % d'`app.py` ne sont pas un aveu : **241 des lignes non couvertes sont du
rendu Streamlit**, intestable hors runtime. Restent 85 lignes de logique, toutes
de faible valeur — l'amorçage de session que `conftest.py` contourne exprès, les
branches `st.secrets`, et les `st.error` de fichier illisible. Ce qui compte, la
plomberie du traitement et les chemins d'échec, est couvert.

## Deux choix à connaître

**Le contrôle « pas de streamlit » tourne dans un sous-processus.** Les anciens
scripts faisaient `assert "streamlit" not in sys.modules` en cours de processus,
ce qui ne tenait que parce que chaque script avait son propre interpréteur. Sous
pytest il suffit qu'un autre module ait légitimement importé streamlit avant pour
que l'assertion devienne fausse sans que rien ne soit cassé. Le sous-processus
mesure la propriété réelle, quel que soit l'ordre des tests.

**L'achèvement désordonné est obtenu par construction, pas par timing.**
`faux.Inverseur` apparie les appels et laisse le second arrivé repartir le
premier. L'ancienne version dormait `0.05 if page % 2 else 0.01` et pariait sur
l'ordonnanceur : le test « l'ordre document est rétabli » ne valait que si le pari
tenait.

## Mesure de qualité

`eval_rex.py` n'est pas un test mais un **script**, à lancer à la main avant et
après une modification de prompt ou de schéma :

```bash
.venv/bin/python tests/eval_rex.py --journal            # 1 appel de segmentation
.venv/bin/python tests/eval_rex.py --rejouer <fichier>  # renote hors ligne, 0 appel
```

Il rejoue le découpage depuis `fixtures/ocr-18p.json` et le compare à
`fixtures/verite-18p.json`. Les notes s'empilent dans `evals/journal.jsonl`, qui
ne contient que des nombres et des empreintes — un changement de prompt montre
donc son delta directement dans le diff.

Les deux fixtures sont **versionnées** ; le PDF source est lui-même dans le dépôt,
donc figer son OCR ne divulgue rien de plus, et c'est ce qui rend la suite
exécutable sur un clone neuf sans clé API. Toute autre charge OCR reste ignorée
par `.gitignore` : ajoutez une exception explicite, jamais un dossier entier.
