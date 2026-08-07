"""
Bonne formation des PDF de `tests/corpus/` — hors ligne, aucun OCR, aucun appel.

Les mêmes recueils synthétiques que `test_corpus.py`, mais en PDF : c'est ce que
le pipeline OCR RÉEL relit dans `eval_corpus_pdf.py`. Ici on garantit seulement
que chaque PDF existe, est un vrai PDF, et a le nombre de pages annoncé par sa
vérité terrain — sans quoi le test live n'aurait aucun sens. Le CONTENU (frontières
de fiches, distracteurs) est déjà garanti par `test_corpus.py` sur le JSON, dont le
PDF est le rendu fidèle (une page markdown → une page PDF, cf. `_generateur.py`).
"""
import json
import re
from pathlib import Path

import pytest

CORPUS = Path(__file__).resolve().parent / "corpus"
FICHIERS = sorted(CORPUS.glob("corpus_*.json"))


def _compte_pages_pdf(octets):
    """
    Nombre de pages d'un PDF matplotlib (v1.4, objets en clair, non compressés) :
    les objets `/Type /Page` — en excluant `/Type /Pages`, le nœud racine de
    l'arbre des pages, d'où le refus d'un « s » juste après.
    """
    return len(re.findall(rb"/Type\s*/Page(?![s])", octets))


def test_le_generateur_a_produit_les_pdf():
    assert FICHIERS, "aucun corpus — lancer tests/corpus/_generateur.py"
    manifeste = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    assert manifeste and all("pdf" in e for e in manifeste), \
        "manifeste sans entrée pdf — régénérer avec _generateur.py"


@pytest.fixture(params=FICHIERS, ids=lambda p: p.stem)
def corpus(request):
    return json.loads(request.param.read_text(encoding="utf-8"))


def test_pdf_existe_et_est_un_vrai_pdf(corpus):
    pdf = CORPUS / f"{corpus['nom']}.pdf"
    assert pdf.exists(), f"{pdf.name} manquant — régénérer avec _generateur.py"
    octets = pdf.read_bytes()
    assert octets.startswith(b"%PDF-"), "en-tête PDF absent"
    assert len(octets) > 1000, "PDF suspicieusement petit"


def test_pdf_a_le_nombre_de_pages_de_la_verite(corpus):
    """
    Une page markdown = une page PDF : le compte doit coller à la vérité terrain,
    sinon l'OCR verrait un autre document que celui que la vérité décrit, et le
    score live ne voudrait rien dire.
    """
    octets = (CORPUS / f"{corpus['nom']}.pdf").read_bytes()
    assert _compte_pages_pdf(octets) == corpus["verite"]["nb_pages"]
