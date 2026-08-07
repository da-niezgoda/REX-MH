"""
Bonne formation des faux recueils de `tests/corpus/` — hors ligne, aucun appel.

Ces fixtures alimentent le test de groupe live `tests/eval_corpus.py`. Ici on ne
juge PAS la segmentation : on garantit que chaque corpus est cohérent et que sa
vérité terrain est exacte, sans quoi le score live ne voudrait rien dire. Le
contrôle central : fiches réelles et pages hors-projet forment une PARTITION des
pages — toute page est classée une fois et une seule.
"""
import json
from pathlib import Path

import pytest

CORPUS = Path(__file__).resolve().parent / "corpus"
FICHIERS = sorted(CORPUS.glob("corpus_*.json"))


def test_le_generateur_a_produit_des_corpus():
    assert FICHIERS, "aucun corpus — lancer tests/corpus/_generateur.py"
    manifeste = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifeste) == len(FICHIERS) == 6


@pytest.fixture(params=FICHIERS, ids=lambda p: p.stem)
def corpus(request):
    return json.loads(request.param.read_text(encoding="utf-8"))


def _pages(debut, fin):
    return set(range(debut, fin + 1))


def test_pages_ocr_contigues_1_indexables(corpus):
    pages = corpus["ocr"]["pages"]
    assert [p["index"] for p in pages] == list(range(len(pages)))
    assert corpus["verite"]["nb_pages"] == len(pages)


def test_le_compte_de_rex_est_precis(corpus):
    """Le nombre annoncé de REX est exactement celui de la vérité terrain."""
    fiches = corpus["verite"]["fiches_attendues"]
    assert corpus["rex"] == len(fiches)
    assert f"_{corpus['rex']}rex" in corpus["nom"]


def test_fiches_dans_les_bornes_disjointes_et_ordonnees(corpus):
    n = corpus["verite"]["nb_pages"]
    fiches = corpus["verite"]["fiches_attendues"]
    dernier = 0
    for f in fiches:
        assert 1 <= f["page_debut"] <= f["page_fin"] <= n
        assert f["page_debut"] > dernier, "fiches non ordonnées ou chevauchantes"
        dernier = f["page_fin"]


def test_fiches_et_hors_projet_partitionnent_les_pages(corpus):
    """
    Invariant central : chaque page est SOIT une fiche réelle, SOIT hors-projet,
    jamais les deux, jamais aucune. C'est ce qui rend la vérité terrain exacte.
    """
    n = corpus["verite"]["nb_pages"]
    fiches = corpus["verite"]["fiches_attendues"]
    hors = corpus["verite"]["hors_projet"]

    pages_fiches = set()
    for f in fiches:
        p = _pages(f["page_debut"], f["page_fin"])
        assert not (pages_fiches & p), "deux fiches se recouvrent"
        pages_fiches |= p

    pages_hors = set()
    for groupe in hors:
        p = set(groupe["pages"])
        assert not (pages_hors & p), "deux groupes hors-projet se recouvrent"
        pages_hors |= p

    assert not (pages_fiches & pages_hors), "une page est à la fois fiche et hors-projet"
    assert pages_fiches | pages_hors == set(range(1, n + 1)), "une page n'est pas classée"


def test_entetes_de_fiche_reperables(corpus):
    """
    Chaque fiche s'ouvre sur un titre `#` ; ses pages de continuation n'en portent
    pas. Sans ce signal, la segmentation n'aurait aucune frontière à trouver — et
    le test live ne mesurerait rien.
    """
    pages = [p["markdown"] for p in corpus["ocr"]["pages"]]
    for f in corpus["verite"]["fiches_attendues"]:
        premiere = pages[f["page_debut"] - 1]
        assert premiere.lstrip().startswith("# "), \
            f"{f['reference']} sans titre en page {f['page_debut']}"
        for num in range(f["page_debut"] + 1, f["page_fin"] + 1):
            assert not pages[num - 1].lstrip().startswith("# "), \
                f"page de continuation {num} porte un titre de niveau 1"


def test_le_sommaire_est_un_distracteur_present(corpus):
    """Dès qu'il y a un sommaire, il est bien hors-projet et nommé comme tel."""
    pages = [p["markdown"] for p in corpus["ocr"]["pages"]]
    sommaires = [i + 1 for i, md in enumerate(pages)
                 if "Liste des actions décrites" in md]
    hors = {p for g in corpus["verite"]["hors_projet"] for p in g["pages"]}
    for page in sommaires:
        assert page in hors, "le sommaire doit être classé hors-projet"
