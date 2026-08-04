"""
`pipeline.py`, `store.py` et `conformite.py` ne doivent JAMAIS importer streamlit.

Ce n'est pas une règle de style, c'est un mécanisme. Un thread de travail sans
`ScriptRunContext` qui lit `st.session_state` n'obtient pas une erreur claire :
Streamlit lui rend un mock GLOBAL AU PROCESSUS. Toutes les fiches échouent alors
identiquement sur `AttributeError: st.session_state has no attribute "REXPrompt"`,
et le mock est partagé entre sessions de navigateur. Rendre `st` inatteignable
depuis ces modules est ce qui empêche cette classe de bug.

Vérifié dans un SOUS-PROCESSUS, et c'est le point de cette version. Les anciens
scripts faisaient `assert "streamlit" not in sys.modules` en cours de processus,
ce qui ne tenait que parce que chaque script avait son propre interpréteur : sous
pytest, il suffit qu'un autre module de test ait légitimement importé streamlit
avant pour que l'assertion devienne fausse sans que rien ne soit cassé. Un
sous-processus mesure la propriété réelle, quel que soit l'ordre des tests.
"""
import subprocess
import sys

import pytest

from conftest import RACINE


@pytest.mark.parametrize("module", ["pipeline", "store", "conformite"])
def test_module_n_importe_pas_streamlit(module):
    code = (
        f"import {module}, sys; "
        f"assert 'streamlit' not in sys.modules, "
        f"'{module}.py a importé streamlit'"
    )
    acheve = subprocess.run([sys.executable, "-c", code], cwd=RACINE,
                            capture_output=True, text=True)
    assert acheve.returncode == 0, acheve.stderr


def test_extraction_concurrente_n_importe_pas_streamlit():
    """
    Le chemin d'exécution lui-même, pas seulement l'import : on fait tourner un
    fan-out complet dans un sous-processus propre et on vérifie qu'aucun worker
    n'a fait entrer streamlit.
    """
    code = (
        "import sys; sys.path.insert(0, 'tests'); "
        "import test_concurrence as t; "
        "c = t.client(); t.lancer(c, 5); "
        "assert 'streamlit' not in sys.modules, 'un worker a importé streamlit'"
    )
    acheve = subprocess.run([sys.executable, "-c", code], cwd=RACINE,
                            capture_output=True, text=True)
    assert acheve.returncode == 0, acheve.stderr
