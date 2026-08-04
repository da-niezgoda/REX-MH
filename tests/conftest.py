"""
Aiguillages communs à la suite. Aucun appel API, aucune clé nécessaire.

Trois pièges du dépôt sont neutralisés ici une fois pour toutes, là où les trois
anciens scripts les traitaient chacun à sa façon :

1. **Chemins relatifs.** `app.load_schema("REX.schema.json")`, `styles.css` et les
   prompts sont lus relativement au répertoire courant, ce qui obligeait à lancer
   les scripts depuis la racine du dépôt. On y va une fois pour toutes.

2. **`load_dotenv()` (app.py) n'écrase PAS une variable déjà posée.** La clé
   factice doit donc être en place AVANT tout `import app`, sinon la vraie clé du
   `.env` gagne et un test pourrait partir vers l'API. On l'écrase de force, y
   compris si le développeur en a exporté une : aucun test ne doit pouvoir
   appeler Mistral.

3. **L'état Streamlit en mode « bare » est global AU PROCESSUS**, partagé par
   tous les tests, et `@st.cache_resource` aussi. Sans remise à zéro, un test qui
   pose un schéma ou échauffe une clé de cache contamine le suivant.

Les points 1 et 2 sont réglés au niveau du MODULE, pas dans une fixture : les
modules de test sont importés pendant la collecte, donc avant l'exécution de la
moindre fixture.
"""
import os
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(RACINE))
os.chdir(RACINE)
os.environ["MISTRAL_API_KEY"] = "factice-pour-les-tests"


@pytest.fixture(scope="module")
def db_temporaire(tmp_path_factory):
    """
    Base SQLite jetable, une par module de test.

    `store._DB_PATH` est une globale de module posée par `init_db`, donc une
    fixture par module suffit — pytest n'entrelace pas les modules. Remplace le
    `tempfile.mkdtemp()` jamais nettoyé des anciens scripts.
    """
    chemin = tmp_path_factory.mktemp("rex") / "rex.db"
    os.environ["REX_DB_PATH"] = str(chemin)
    import store

    store.init_db(str(chemin))
    return str(chemin)


@pytest.fixture
def db_neuve(tmp_path):
    """
    Base vierge pour UN test.

    Nécessaire dès qu'un test affirme quelque chose de global — « plus aucun
    document », « les stats sont inchangées » : de telles assertions ne tiennent
    que sur une base dont le test est seul propriétaire. Créer les tables coûte
    une poignée de millisecondes.
    """
    chemin = tmp_path / "rex.db"
    os.environ["REX_DB_PATH"] = str(chemin)
    import store

    store.init_db(str(chemin))
    return str(chemin)


def nettoyer_caches_streamlit():
    """
    Vide les caches Streamlit globaux au processus.

    `obtenir_client` et `_cles_echauffees` sont sous `@st.cache_resource` : sans
    ce nettoyage, l'ensemble des clés déjà échauffées survit d'un test à l'autre
    et le comportement de `deja_echauffe` dépend de l'ordre d'exécution.
    """
    import app

    for fonction in (app.load_text_file, app.obtenir_client, app._cles_echauffees):
        try:
            fonction.clear()
        except Exception:
            pass


@pytest.fixture(scope="module")
def contexte_app():
    """
    Prompts et schémas posés dans `st.session_state`, comme le fait l'application
    avant tout fan-out.

    On écrit dans `st.session_state` hors runtime Streamlit : c'est précisément le
    mock global au processus décrit en tête de fichier. Danger en production,
    commodité ici — mais il faut le rendre et le nettoyer, d'où cette fixture.
    """
    import streamlit as st

    import app

    nettoyer_caches_streamlit()
    avant = {cle: st.session_state.get(cle) for cle in app.CLES_PROMPTS}

    st.session_state.REXSchema = app.load_schema("REX.schema.json")
    st.session_state.REXListSchema = app.load_schema("REXlist.schema.json")
    st.session_state.REXPrompt = app.load_prompt("REXPrompt.md", st.session_state.REXSchema)
    st.session_state.listPrompt = app.load_prompt("listPrompt.md",
                                                  st.session_state.REXListSchema)
    st.session_state.vocabulaire = app.load_vocabulaire()
    # Le vocabulaire peut légitimement être vide (la canonicalisation seule suffit),
    # contrairement aux prompts et aux schémas.
    for cle in app.CLES_PROMPTS:
        assert cle in st.session_state, f"{cle} non initialisé"
    assert all(st.session_state[cle] for cle in app.CLES_PROMPTS if cle != "vocabulaire"), \
        "prompts ou schémas illisibles — le répertoire courant est-il la racine ?"

    yield st.session_state

    for cle, valeur in avant.items():
        if valeur is None:
            st.session_state.pop(cle, None)
        else:
            st.session_state[cle] = valeur
    nettoyer_caches_streamlit()


@pytest.fixture(scope="session")
def schema_rex():
    """Le schéma REX chargé une fois, sans Streamlit ni session_state."""
    import json

    return json.loads((RACINE / "REX.schema.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def index_conformite(schema_rex):
    """Index de conformité bâti sur le schéma et le vocabulaire réels."""
    import json

    import conformite

    vocabulaire = json.loads((RACINE / "vocabulary.json").read_text(encoding="utf-8"))
    index, _ = conformite.construire_index(schema_rex, vocabulaire)
    return index
