# 🌿 REX-MH — Extraction de Retours d'Expérience « Zones Humides »

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
   ├─▶ 1. Upload vers l'API Mistral (files.upload, purpose="ocr")
   │
   ├─▶ 2. OCR document complet  (mistral-ocr-latest)
   │        → une liste de pages { page_number, content(markdown) }
   │
   ├─▶ 3. Segmentation  (mistral-medium-latest + listPrompt.md + REXlist.schema.json)
   │        → { "Liste": [ { Titre, PageDebut, PageFin }, … ] }
   │
   ├─▶ 4. Pour chaque projet de la liste :
   │        - découpe des pages du segment (clean_pages)
   │        - extraction structurée (mistral-medium-latest + REXPrompt.md + REX.schema.json)
   │        → un objet JSON par projet, enrichi de _project_title / _page_debut / _page_fin
   │
   ├─▶ 5. Affichage : tableau Material UI (st_mui_table) avec ligne dépliable par projet
   │
   └─▶ 6. Export : aplatissement des sections en colonnes → Excel (xlsxwriter)
```

Points de conception notables :

- **Deux passes LLM plutôt qu'une.** Un recueil complet dépasse le contexte utile d'un seul appel et
  dilue l'attention du modèle. La première passe ne fait que du découpage (titre + plage de pages), la
  seconde ne voit que les pages d'un seul projet. Cela améliore nettement la précision d'extraction.
- **Les schémas JSON pilotent les prompts.** Les fichiers `*.schema.json` sont injectés à la place du
  placeholder `{{ SCHEMA_JSON }}` dans les prompts Markdown (`load_prompt`). Faire évoluer le modèle de
  données = éditer le schéma, sans toucher au code Python.
- **`temperature=0.0` et `response_format={"type": "json_object"}`** pour maximiser la reproductibilité.
- **Tolérance aux pannes partielles :** un projet dont le JSON est invalide ou dont l'appel échoue est
  ignoré (avertissement en console) sans faire échouer le traitement du reste du document.
- **Progression :** un `progress_callback` remonte l'avancement (upload → OCR → segmentation → n
  extractions) dans la barre de progression Streamlit.

---

## Structure du projet

| Fichier | Rôle |
| --- | --- |
| `app.py` | Application Streamlit complète : chargement des schémas/prompts, pipeline OCR + LLM, rendu du tableau, export Excel. |
| `REXPrompt.md` | Prompt système d'**extraction** d'une fiche projet (rôle, format d'entrée page par page, gestion des champs absents → `""`). |
| `listPrompt.md` | Prompt système de **segmentation** du recueil (identifier l'introduction/annexes, détecter les ruptures, définir `PageDebut`/`PageFin`). |
| `REX.schema.json` | Modèle de données d'une fiche projet : 10 sections (`Presentation`, `Typologie`, `Enjeux`, `Directives`, `Travaux`, `Contexte`, `Objectif`, `Description`, `Valorisation`, `Documents`) avec descriptions détaillées et **énumérations métier** (23 régions, 11 types d'ingénierie écologique, 43 types de milieux Ramsar, 13 typologies SDAGE, 11 typologies hydrogéomorphologiques Sandre, 53 techniques de génie écologique, 15 enjeux, 17 statuts de protection…). |
| `REXlist.schema.json` | Modèle de la liste de segments (`Liste[] : Titre, PageDebut, PageFin`). |
| `styles.css` | Thème « Material 3 Expressive » eau & biodiversité (variables CSS, en-tête, cartes, uploader, boutons, messages). |
| `requirements.txt` | Dépendances Python épinglées. |
| `.devcontainer/devcontainer.json` | Dev container / Codespaces : Python 3.11, installation des requirements, lancement automatique de Streamlit sur le port 8501. |
| `IFD_FICJOINT_0020373.PDF`, `IFD_FICJOINT_0020373-1-18.pdf` | Documents d'exemple servant de jeu de test (le second est un extrait des pages 1 à 18, plus rapide à traiter). |

---

## Outils et technologies

| Domaine | Choix |
| --- | --- |
| Langage | Python 3.11 |
| Interface | [Streamlit](https://streamlit.io) 1.49 |
| Tableau | `st-mui-table` (composant Material UI, lignes dépliables via `detailColumns`) |
| OCR | API Mistral — `mistral-ocr-latest` (sortie Markdown page par page) |
| Extraction / LLM | API Mistral — `mistral-medium-latest`, `temperature=0`, sortie JSON forcée |
| Contrat de données | JSON Schema draft-07 |
| Données / export | `pandas`, `xlsxwriter` (Excel) |
| Style | CSS personnalisé injecté via `st.html` |
| Environnement | Dev Container (`mcr.microsoft.com/devcontainers/python:1-3.11-bookworm`), déployable sur Streamlit Community Cloud |

Dépendances présentes mais non encore utilisées dans `app.py` : `plotly`, `requests`, `streamlit-extras`
(vestiges d'itérations antérieures / usages prévus).

---

## Installation et lancement

```bash
pip install -r requirements.txt
```

Renseigner la clé API Mistral dans `.streamlit/secrets.toml` (lue via `st.secrets['MISTRAL_API_KEY']`) :

```toml
MISTRAL_API_KEY = "..."
```

Puis :

```bash
streamlit run app.py
```

L'application est disponible sur http://localhost:8501. En Codespaces / Dev Container, Streamlit est
lancé automatiquement à l'attachement du conteneur et le port 8501 est ouvert en aperçu.

> ⚠️ `.streamlit/secrets.toml` ne doit jamais être commité — il est ignoré par le `.gitignore`,
> tout comme `.claude/` (configuration locale de l'agent).

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

Le dépôt est propre sur `main`, aucun travail en cours non commité.

---

## Limites connues / pistes d'amélioration

- Pas d'exemple de secrets versionné (`.streamlit/secrets.toml.example`).
- Pas de validation programmatique du JSON renvoyé par le LLM contre `REX.schema.json` (seul le prompt
  garantit la conformité) : un champ hors énumération passe silencieusement.
- Aucune persistance : le résultat vit dans `st.session_state` et disparaît au rechargement.
- Traitement séquentiel des projets — un recueil de 50 fiches enchaîne 50 appels LLM.
- Le fallback d'affichage du tableau lit `project["presentation"]["titre"]` (minuscules) alors que le
  schéma produit `Presentation` / `Titre` : ce chemin d'erreur retombe donc sur « Projet N ».
- Les deux schémas utilisent `additionnalProperties` (faute de frappe) au lieu de
  `additionalProperties` dans `REXlist.schema.json` — sans effet côté validation.
- Aucun test automatisé.
- Les deux PDF d'exemple (10,6 Mo) sont versionnés dans le dépôt.
