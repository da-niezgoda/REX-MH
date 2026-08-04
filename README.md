# 🌿 REX-MH — Extraction de Retours d'Expérience « Zones Humides »

**Application en ligne : [rex-mh-oieau.streamlit.app](https://rex-mh-oieau.streamlit.app)**

Application **Streamlit** qui transforme un recueil PDF de retours d'expérience (REX) sur des projets
de gestion, restauration ou conservation de **zones humides** en données structurées, exportables en Excel.

L'utilisateur dépose un PDF (souvent un recueil de plusieurs dizaines de fiches projet) ; l'application
en fait l'OCR, le découpe automatiquement en fiches projet, extrait pour chaque fiche un ensemble
normalisé de champs (bassin, région, typologie d'ingénierie écologique, masses d'eau DCE, Natura 2000,
enjeux, dates, surfaces, valorisation…), puis affiche le résultat dans un tableau dépliable et propose
un export `.xlsx`.

---

## Fonctionnement

```
PDF déposé
   │
   ├─▶ 1. Empreinte du contenu (sha256) → cache OCR consulté
   │        si le PDF a déjà été océrisé : on passe directement à l'étape 4
   │
   ├─▶ 2. Upload vers l'API Mistral (files.upload, purpose="ocr"), puis suppression
   │
   ├─▶ 3. OCR document complet  (mistral-ocr-4-0)
   │        → pages { page_number, content(markdown) }, blocs structurels,
   │          en-têtes/pieds séparés, score de confiance par page
   │        → charge gzippée en base : les traitements suivants sont gratuits
   │
   ├─▶ 4. Segmentation  (mistral-small-latest + listPrompt.md + REXlist.schema.json)
   │        → { "Liste": [ { Titre, PageDebut, PageFin }, … ] }
   │        → bornes validées : un segment hors document ou à page 0 est refusé
   │          AVANT d'être payé, et remonte comme échec nommé
   │
   ├─▶ 5. Extraction, selon le mode choisi :
   │        « rapide »     → 1 fiche seule (amorçage du cache de prompt) puis
   │                         les autres en parallèle (4 à la fois)
   │        « économique » → un travail par lot (-50 %), à récolter plus tard
   │        (mistral-medium-latest + REXPrompt.md + REX.schema.json)
   │        → un objet JSON par fiche, enrichi de _project_title / _page_debut /
   │          _page_fin / _model_* / _prompt_hash
   │
   ├─▶ 6. Persistance SQLite : document, traitement, une ligne par fiche
   │        (y compris les fiches en échec, pour pouvoir les relancer)
   │
   ├─▶ 7. Affichage : tableau Material UI (st_mui_table) avec ligne dépliable,
   │        panneau des fiches en échec, onglet Historique
   │
   └─▶ 8. Export : aplatissement des sections en colonnes → Excel (xlsxwriter)
```

Points de conception notables :

- **Deux passes LLM plutôt qu'une.** Un recueil complet dépasse le contexte utile d'un seul appel et
  dilue l'attention du modèle. La première passe ne fait que du découpage (titre + plage de pages), la
  seconde ne voit que les pages d'un seul projet. Cela améliore nettement la précision d'extraction.
- **Les schémas JSON pilotent les prompts.** Les fichiers `*.schema.json` sont injectés à la place du
  placeholder `{{ SCHEMA_JSON }}` dans les prompts Markdown (`load_prompt`). Faire évoluer le modèle de
  données = éditer le schéma, sans toucher au code Python.
- **Sortie structurée stricte.** Les deux appels LLM utilisent
  `response_format={"type": "json_schema", …, "strict": true}` : le modèle ne peut ni inventer une clé
  hors schéma ni sortir d'une énumération. `temperature=0.0` et une graine fixe (`random_seed`)
  complètent la reproductibilité.
- **Deux niveaux de modèle.** `mistral-small-latest` (Small 4) suffit pour découper le recueil,
  `mistral-medium-latest` (Medium 3.5) est réservé à l'extraction. Les trois modèles sont déclarés en
  tête de `app.py` ; la version réellement servie par l'API est enregistrée sur chaque fiche
  (`_model_extraction`) pour que deux extractions restent comparables.
- **Cache de prompt.** Le prompt système (~12 000 jetons, schéma inclus) est identique pour toutes
  les fiches. Une `prompt_cache_key` stable — dérivée de l'empreinte du prompt *rendu* et du modèle —
  fait facturer ces jetons 10 % du tarif dès qu'ils sont servis depuis le cache. Le cache étant
  écrit par le premier appel, la première fiche part **seule** avant que les suivantes
  s'éventaillent : sans cet amorçage, N appels simultanés manqueraient tous le cache.
  Mesuré : en séquentiel, la 2ᵉ fiche atteint **93 %** de jetons de prompt en cache. En parallèle,
  seule la première vague d'appels simultanés manque le cache (les 4 fiches en vol ensemble), toutes
  les suivantes le touchent — le surcoût est donc borné par la limite de concurrence et s'amortit
  d'autant mieux que le recueil est gros (37 % sur 7 fiches, l'essentiel des fiches d'un recueil de
  plusieurs dizaines).
- **Deux modes d'extraction.** « Rapide » parallélise (4 appels en vol) et affiche le résultat tout
  de suite ; « économique » soumet un travail par lot à moitié prix, dont les résultats se récoltent
  plus tard — l'onglet peut être fermé entre-temps. Ils s'excluent : dans un lot, c'est Mistral qui
  ordonnance, il n'y a rien à paralléliser côté client.
- **Rien n'est perdu en silence.** Chaque fiche en échec est enregistrée avec sa plage de pages, sa
  catégorie (`quota`, `timeout`, `json_invalide`, `segment_invalide`, `bug`…) et sa trace, puis
  affichée. Le bouton de relance repart de l'OCR en cache, sans repayer ni OCR ni découpage.
- **Tolérance aux pannes partielles :** une fiche qui échoue n'emporte pas le reste du document. En
  revanche un bug de l'application détecté sur la fiche d'amorçage interrompt le traitement, plutôt
  que de se reproduire N fois.
- **Progression :** un `progress_callback` remonte l'avancement, pondéré par phase (upload 5 %,
  OCR 25 %, segmentation 10 %, extraction 60 %) et compté sur les fiches **terminées** — sous
  parallélisme, compter les envois ferait sauter la barre à 100 % dès la dernière soumission.
- **Historique local (SQLite).** Documents, traitements et fiches sont conservés : on rouvre un
  résultat et on réexporte en Excel sans un seul appel d'API. Une archive ZIP permet de sauvegarder
  et restaurer l'historique — indispensable en déploiement, où le disque est éphémère.

---

## Structure du projet

| Fichier | Rôle |
| --- | --- |
| `app.py` | Interface Streamlit et orchestration : chargement des schémas/prompts, enchaînement OCR → découpage → extraction, onglets Traitement / Historique, rendu du tableau, export Excel. |
| `REXPrompt.md` | Prompt système d'**extraction** d'une fiche projet (rôle, format d'entrée page par page, gestion des champs absents → `""`). |
| `listPrompt.md` | Prompt système de **segmentation** du recueil (identifier l'introduction/annexes, détecter les ruptures, définir `PageDebut`/`PageFin`). |
| `REX.schema.json` | Modèle de données d'une fiche projet : 10 sections (`Presentation`, `Typologie`, `Enjeux`, `Directives`, `Travaux`, `Contexte`, `Objectif`, `Description`, `Valorisation`, `Documents`) avec descriptions détaillées et **énumérations métier** (23 régions, 11 types d'ingénierie écologique, 43 types de milieux Ramsar, 13 typologies SDAGE, 11 typologies hydrogéomorphologiques Sandre, 53 techniques de génie écologique, 15 enjeux, 17 statuts de protection…). |
| `REXlist.schema.json` | Modèle de la liste de segments (`PagesHorsProjet[]`, puis `Liste[] : PageDebut, PageFin, Titre, Motif`). L'ordre des propriétés est **l'ordre de génération** en mode strict : les pages hors projet d'abord, et la justification après les bornes de pages — voir *Limites connues*. |
| `conformite.py` | Normalisation et validation d'une fiche après génération : recalage des valeurs d'énumération approchantes, dédoublonnage des listes, verdict `conforme` / `corrigé` / `non conforme`. N'importe pas `streamlit`. |
| `vocabulary.json` | Vocabulaire contrôlé hors du code : alias de synonymes, réglages de normalisation. Étendu par la tâche 5 (clés d'export, formats, routage). |
| `styles.css` | Thème « Material 3 Expressive » eau & biodiversité (variables CSS, en-tête, cartes, uploader, boutons, messages). |
| `pipeline.py` | Cœur du traitement : client Mistral, construction des requêtes, concurrence, mode par lot, classification des erreurs, comptabilité des jetons. **N'importe pas `streamlit`** (voir *Limites connues*). |
| `store.py` | Persistance SQLite : cache OCR, historique des traitements, fiches, travaux par lot, export/import d'archive. N'importe pas `streamlit` non plus. |
| `smoke_test.py` | Vérification en direct de la chaîne Mistral sur l'extrait 18 pages : blocs structurels, conformité au schéma, champs signalés par le client, et **mesure du cache de prompt** (deux fiches consécutives). Options `--fixture` (hors ligne, sans clé) et `--batch` (répétition du mode par lot). |
| `tests/` | Suite `pytest` hors ligne, sans clé API, plus `eval_rex.py` qui note la qualité du découpage — voir `tests/README.md`. |
| `requirements.txt` | Dépendances Python épinglées (celles du déploiement). |
| `requirements-dev.txt` | `pytest`, en plus des précédentes. Séparé à dessein : le déploiement n'installe que `requirements.txt`. |
| `.devcontainer/devcontainer.json` | Dev container / Codespaces : Python 3.11, installation des requirements, lancement automatique de Streamlit sur le port 8501. |
| `IFD_FICJOINT_0020373.PDF`, `IFD_FICJOINT_0020373-1-18.pdf` | Documents d'exemple servant de jeu de test (le second est un extrait des pages 1 à 18, plus rapide à traiter). |

---

## Outils et technologies

| Domaine | Choix |
| --- | --- |
| Langage | Python 3.10+ (3.11 en dev container) |
| Interface | [Streamlit](https://streamlit.io) 1.60 |
| Tableau | `st-mui-table` (composant Material UI, lignes dépliables via `detailColumns`) |
| SDK | `mistralai` 2.x — attention, l'import est `from mistralai.client import Mistral` |
| OCR | API Mistral — `mistral-ocr-4-0` (Markdown page par page, blocs structurels, en-têtes/pieds, confiance) |
| Segmentation | API Mistral — `mistral-small-latest` (Small 4) |
| Extraction | API Mistral — `mistral-medium-latest` (Medium 3.5), `temperature=0`, `random_seed` fixe |
| Contrat de données | JSON Schema draft-07, imposé au modèle en mode `json_schema` strict |
| Concurrence | `ThreadPoolExecutor` sur le client synchrone (4 extractions en vol) |
| Persistance | SQLite (module standard), mode WAL, charges OCR gzippées |
| Données / export | `pandas`, `xlsxwriter` (Excel), `zipfile` (archive d'historique) |
| Style | CSS personnalisé injecté via `st.html` |
| Environnement | Dev Container (`mcr.microsoft.com/devcontainers/python:1-3.11-bookworm`), déployable sur Streamlit Community Cloud |

Dépendances présentes mais non encore utilisées : `plotly`, `requests`, `streamlit-extras`
(vestiges d'itérations antérieures / usages prévus). La persistance, l'archivage et la concurrence
n'ajoutent aucune dépendance : `sqlite3`, `gzip`, `zipfile`, `html` et
`concurrent.futures` sont dans la bibliothèque standard.

---

## Installation et lancement

Le projet s'installe dans un environnement virtuel dédié. Ce n'est pas cosmétique : `mistralai` 2.x
exige `opentelemetry-semantic-conventions >= 0.60b1`, incompatible avec d'autres outils d'observabilité
LLM qui l'épinglent en `0.59b0`. Un venv évite d'arbitrer entre les deux.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Renseigner la clé API Mistral. En local, dans un fichier `.env` à la racine :

```dotenv
MISTRAL_API_KEY=...
```

En déploiement (Streamlit Community Cloud), la même clé se déclare dans les *secrets* de
l'application. `get_api_key()` regarde `st.secrets` d'abord, puis l'environnement / `.env`, si bien
que le même code fonctionne dans les deux contextes.

Puis :

```bash
.venv/bin/streamlit run app.py
```

L'application est disponible sur http://localhost:8501. En Codespaces / Dev Container, Streamlit est
lancé automatiquement à l'attachement du conteneur et le port 8501 est ouvert en aperçu.

Pour vérifier la chaîne Mistral sans lancer l'interface (peu coûteux, 18 pages) :

```bash
.venv/bin/python smoke_test.py
```

Et les vérifications qui ne coûtent rien, sans clé API :

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Pour mesurer la qualité du découpage — un seul appel `mistral-small-latest`, rejoué depuis la charge
OCR figée, sans OCR ni extraction — et empiler la note dans `tests/evals/journal.jsonl` :

```bash
.venv/bin/python tests/eval_rex.py --journal
```

### Historique et stockage

L'historique est une base SQLite locale, `data/rex.db` par défaut (`REX_DB_PATH` pour en changer,
en variable d'environnement ou en secret Streamlit). Elle contient le cache OCR — c'est ce qui rend
gratuit tout retraitement d'un PDF déjà océrisé.

> ⚠️ **En déploiement, ce stockage est éphémère** : Streamlit Community Cloud repart d'un disque
> vierge à chaque redéploiement. L'onglet *Historique* propose de télécharger une archive ZIP et de
> la restaurer ensuite ; c'est le seul moyen de conserver un jeu de documents traités, cache OCR
> compris. N'importez que des archives dont vous connaissez la provenance : le format ne permet pas
> de vérifier qu'une charge OCR correspond bien au PDF qu'elle prétend décrire.

> ⚠️ `.env` et `.streamlit/secrets.toml` ne doivent jamais être commités — ils sont ignorés par le
> `.gitignore`, tout comme `.claude/`, `data/` et `*.db` (contenu de vrais documents).

---

## Résumé de l'historique Git

Développement mené par une seule personne (`da-niezgoda` / `d.niezgoda`), 24 commits entre le
**4 octobre 2025** et le **29 octobre 2025**.

| Période | Étape |
| --- | --- |
| 4 oct. 2025 | Amorçage : squelette Streamlit, gestion des secrets, dépendances, ajout du Dev Container. |
| 5 oct. 2025 | Travail sur l'identité visuelle (`styles.css`, thème Material 3 eau & biodiversité). |
| 8 oct. 2025 | Ajout des PDF d'exemple (recueil complet + extrait 18 pages) comme jeu de test. |
| 15 oct. 2025 | Cœur du projet : branchement de l'IA (OCR + extraction Mistral), affinage du prompt de segmentation (« better decoupe »), passage de l'export Excel à `xlsxwriter`, itérations sur `app.py`. |
| 17 – 23 oct. 2025 | Précisions et refonte du `REX.schema.json` (descriptions, énumérations métier), nettoyage des `print` / de la température. |
| 29 oct. 2025 | Retouches finales du champ « résumé » du prompt et du schéma. |
| 29 – 30 juil. 2026 | **v2.** Montée en version du SDK (`mistralai` 2.x), OCR 4 et sorties structurées strictes ; puis cache de prompt, extraction parallèle, mode par lot, historique SQLite avec cache OCR, remontée des échecs par fiche et vérifications hors ligne. |

---

## Limites connues / pistes d'amélioration

- **Le sur-découpage est corrigé** : l'extrait de 18 pages renvoyait 7 segments pour 2 vraies fiches,
  il en renvoie 2, aux bornes exactes (note de découpage 0,444 → 1,000). Ce qui a fait le travail
  n'est pas la géométrie mais le fait de rendre l'étape d'exclusion **explicite** — le modèle doit
  désormais énumérer les pages hors projet dans `PagesHorsProjet` au lieu de les « identifier
  mentalement » — et de nommer dans `listPrompt.md` les cas réels : sommaire, liste des opérations,
  carte de localisation, et la page de *fiche-type* annotée (une légende de mise en page qui contient
  un spécimen complet, avec un vrai titre et un vrai montant). Les blocs structurels de l'OCR 4
  restent demandés et cachés, mais **inutilisés** : aucun besoin de géométrie ne s'est matérialisé.
- **L'échauffement du cache est sauté en dessous de 3 fiches** (`SEUIL_ECHAUFFEMENT`). Il sérialise le
  premier appel pour n'économiser 90 % que sur *une* fiche : avec 2 fiches, le run devient entièrement
  séquentiel pour un gain négligeable. Médiane mesurée sur l'extrait de 18 pages : ~65 s avec
  échauffement, ~34 s sans. La variance de l'API étant forte (31 à 75 s pour un travail identique),
  ne concluez jamais sur un seul run.
- ⚠️ **`PagesHorsProjet` est un outil de raisonnement, pas une donnée : ne rien en déduire.** Sur le
  recueil complet, le modèle a déclaré 110 pages hors projet — dont 29 à 129 — alors que ses propres
  segments couvrent 37 à 129 ; le vrai ensemble est {1-9, 29-36}. Il se contredit donc sur un long
  document, en étendant paresseusement un intervalle au lieu de raisonner page par page. Sans
  conséquence aujourd'hui, car rien ne le lit (`_segmenter` ne prend que `Liste`), et le champ remplit
  malgré tout son office : c'est le fait d'**exiger** cette exclusion qui a corrigé le sur-découpage.
  Mais le recoupement « pages non couvertes = pages déclarées hors projet » ne tient pas à l'échelle.
- ⚠️ **`Motif` doit rester déclaré APRÈS `PageDebut`/`PageFin`** dans `REXlist.schema.json`. L'ordre
  des propriétés est l'ordre de génération : en le plaçant avant, le modèle justifiait un segment
  avant d'avoir choisi ses pages, s'ancrait sur le contenu de la page 4 et donnait aux pages 10-14 le
  titre et le maître d'ouvrage du spécimen de la légende. Les bornes de pages étaient pourtant
  parfaites — seul le libellé était contaminé, ce qu'une note de découpage seule ne voit pas (d'où le
  contrôle de recouvrement de titre dans `eval_rex.py`).
- Le schéma est envoyé deux fois par appel (injecté dans le prompt via `{{ SCHEMA_JSON }}` **et**
  passé en `response_format`), soit ~12 000 jetons d'entrée par fiche. Redondance assumée : avec le
  cache de prompt, ces jetons sont facturés 10 % dès la deuxième fiche, si bien que retirer la copie
  du prompt ne rapporterait plus grand-chose face au risque qualité.
- Le mode strict de Mistral n'accepte pas `anyOf`, `format`, `uniqueItems`, `$ref`, `oneOf`, `allOf`
  (erreur 400 / code 3051). Ces mots-clés ont été retirés du schéma au profit de `pattern` ;
  conséquence : les doublons dans `type_valorisation` ne sont plus interdits par le schéma — ils sont
  dédoublonnés par `conformite.py`, qui préserve l'ordre.
- **Chaque fiche porte désormais un verdict** (`conforme` / `corrigé` / `non conforme`) dans la base
  et dans deux colonnes de l'export Excel, avec le journal complet des recalages (`avant`, `après`,
  règle appliquée). `status` et `validation_status` sont deux axes distincts : une fiche non conforme
  reste `status='ok'`, sinon elle disparaîtrait précisément de l'écran où un expert doit la corriger.
- **La normalisation ne recale que sur une correspondance exacte après canonicalisation**, plus une
  table d'alias tenue à la main. Jamais de distance d'édition : la clé canonique est vérifiée
  *injective* sur les 204 valeurs d'énumération à chaque exécution des tests, si bien qu'un recalage
  est une preuve que la sortie du modèle et la valeur métier ne diffèrent que par la casse, les
  accents, les apostrophes, la ponctuation ou un pluriel. Une valeur non résolue est **laissée telle
  quelle** et signalée — la couche ne devine pas. `type_genie_ecologique` contient `Fauche`,
  `Fenaison et pâture` et `Pâturage` : une distance d'édition y corromprait des données expertes sans
  qu'on puisse dire lesquelles.
- ⚠️ Deux alias visent `Site Natura 2000`, valeur qui **n'existe pas encore** dans l'énumération
  `Contexte.contexte`. Ce n'est pas une erreur fatale : le point est signalé dans le popover
  Maintenance, et la tâche 5 ajoutera la valeur. Un alias mal orthographié, en revanche, fait échouer
  la suite de tests.
- `pipeline.py`, `store.py` et `conformite.py` n'importent délibérément pas `streamlit`. Ce n'est pas une préférence
  de style : un thread de travail sans `ScriptRunContext` qui lit `st.session_state` ne reçoit pas
  d'erreur claire — Streamlit lui sert un état factice **global au processus**, si bien que toutes
  les fiches échouent identiquement sur un `AttributeError` et que l'interface n'affiche qu'« aucun
  projet analysé ». Rendre `st` inaccessible dans ces modules empêche ce bug par construction.
- **L'historique est global et sans authentification.** Sur l'application publique, tout visiteur
  voit, exporte et peut **supprimer** les documents traités par les autres, et peut importer une
  archive qui empoisonnerait le cache OCR. Choix assumé pour un usage interne ; un mot de passe
  (`st.secrets["APP_PASSWORD"]`) en tête de `main()` suffirait à fermer l'accès.
- `flatten_project_data()` aplatit les sections sur le nom feuille des champs, sans préfixe : deux
  sections partageant un nom de champ s'écraseraient dans l'export Excel. Les noms nus étant le
  contrat de colonnes du client, le schéma d'aplatissement est conservé et l'invariant est **verrouillé
  par un test** — ajouter un doublon fait échouer la suite au lieu de faire disparaître une colonne.
- `st-mui-table` n'a plus de version depuis janvier 2024, et rend dans son propre iframe — d'où le
  jeu de jetons Material 3 dupliqué en `customCss` dans `app.py`. La refonte d'interface le remplacera
  par des composants Streamlit natifs.
- `format_expanded_data()` construit du HTML à la main (échappé, mais à la main) : une refonte
  pilotée par le schéma éviterait d'avoir à toucher au rendu pour chaque nouveau champ.
- La note `score_rex_v1` combine **trois** composantes seulement — découpage, conformité, remplissage.
  La correspondance champ par champ demande une vérité terrain que seuls les experts du client peuvent
  fournir : `tests/fixtures/verite-18p.json` porte donc `champs: {}`, et le scoreur annonce ce qu'il
  ne mesure pas. La métrique est **versionnée** plutôt que renormalisée : quand ces étiquettes
  arriveront elles feront un `v2`, et un v1 ne se compare jamais à un v2.
- Les deux PDF d'exemple (10,6 Mo) sont versionnés dans le dépôt — ce qui mériterait une décision en
  soi, s'agissant de documents du client. C'est aussi ce qui justifie de versionner la charge OCR
  figée de l'extrait : elle ne divulgue rien que le PDF ne divulgue déjà, et elle rend la suite
  exécutable sur un clone neuf sans clé API.
- Pas d'exemple de secrets versionné (`.streamlit/secrets.toml.example`).
