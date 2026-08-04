"""
Le scoreur lui-même. Hors ligne : toutes les fonctions notées sont pures, et
c'est délibéré — la partie réseau d'`eval_rex.py` se réduit à un appel.

Un scoreur non testé est un scoreur qui dérive, et une métrique qui dérive est
pire qu'absente : elle donne l'impression de mesurer un progrès.
"""
import json

import pytest

import eval_rex
from fabrique import fiche_conforme

VERITE = {
    "document_sha256": "0" * 64,
    "nb_pages": 18,
    "fiches_attendues": [
        {"reference": "P.1", "titre_indicatif": "Marais de Villiers",
         "page_debut": 10, "page_fin": 14},
        {"reference": "P.2", "titre_indicatif": "Étang de Bracieux",
         "page_debut": 15, "page_fin": 18},
    ],
    "hors_projet": [{"pages": list(range(1, 10)), "motif": "introduction"}],
    "champs": {},
}


def _segment(debut, fin, titre="x"):
    return {"PageDebut": debut, "PageFin": fin, "Titre": titre, "Motif": "y"}


PARFAIT = [_segment(10, 14, "Marais de Villiers"),
           _segment(15, 18, "Étang de Bracieux")]


# --- Découpage ---------------------------------------------------------------


def test_decoupage_parfait():
    note = eval_rex.noter_decoupage(PARFAIT, VERITE)
    assert (note["exactes"], note["perdues"], note["fragments"]) == (2, 0, 0)
    assert note["fantomes"] == []
    assert note["precision"] == note["rappel"] == note["iou_moyen"] == 1.0
    assert note["score"] == 1.0


def test_fantome_fait_chuter_la_precision():
    """Le défaut mesuré : 7 segments pour 2 fiches réelles."""
    segments = [_segment(1, 3), _segment(4, 4), _segment(5, 6), _segment(7, 8),
                _segment(9, 9)] + PARFAIT
    note = eval_rex.noter_decoupage(segments, VERITE)
    assert len(note["fantomes"]) == 5
    assert note["perdues"] == 0 and note["exactes"] == 2
    assert note["rappel"] == 1.0
    assert note["precision"] == pytest.approx(2 / 7)
    assert note["score"] < 0.5


def test_fiche_perdue_est_l_issue_fatale():
    note = eval_rex.noter_decoupage([_segment(10, 14)], VERITE)
    assert note["perdues"] == 1
    assert note["rappel"] == 0.5
    detail = next(d for d in note["detail"] if d["reference"] == "P.2")
    assert detail["perdue"] is True and detail["iou"] == 0.0


def test_fragmentation_comptee():
    """Une fiche réelle coupée en deux : rien de perdu, mais un appel de trop."""
    segments = [_segment(10, 12), _segment(13, 14), _segment(15, 18)]
    note = eval_rex.noter_decoupage(segments, VERITE)
    assert note["fragments"] == 1 and note["perdues"] == 0
    assert note["fantomes"] == []


def test_bornes_approximatives_notees_par_iou():
    note = eval_rex.noter_decoupage([_segment(10, 13), _segment(15, 18)], VERITE)
    assert note["exactes"] == 1 and note["couvertes"] == 2
    assert 0.7 < note["iou_moyen"] < 1.0


def test_recouvrement_de_titre_detecte_la_contamination():
    """
    Bornes parfaites, libellé emprunté à une autre page : le cas réellement
    observé, que la note de découpage seule ne voit pas.
    """
    assert eval_rex.recouvrement_titre("Marais de Villiers",
                                       "Marais de Villiers") == 1.0
    assert eval_rex.recouvrement_titre("Reculées de la Moselle",
                                       "Marais de Villiers") == 0.0
    # Les mots vides ne comptent pas, et les accents sont ignorés.
    assert eval_rex.recouvrement_titre("etang de bracieux",
                                       "Étang de Bracieux") == 1.0


def test_pages_hors_projet_aplaties():
    assert eval_rex.pages_hors_projet(VERITE) == set(range(1, 10))


# --- Conformité et remplissage ----------------------------------------------


def test_conformite_ne_compte_que_conforme():
    """« corrigé » n'est pas propre : la barre est zéro recalage."""
    note = eval_rex.noter_conformite(["conforme", "conforme", "corrige",
                                      "non_conforme"])
    assert note["score"] == 0.5 and note["fiches"] == 4
    assert note["comptes"]["corrige"] == 1


def test_conformite_absente_quand_aucun_verdict():
    """Un run antérieur à la tâche 3 ne porte pas de verdict."""
    assert eval_rex.noter_conformite([None, None]) is None
    assert eval_rex.noter_conformite([]) is None


def test_remplissage_sur_une_fiche_pleine(schema_rex):
    fiche = fiche_conforme(schema_rex)
    fiche["Presentation"]["Titre"] = "Un titre"
    note = eval_rex.noter_remplissage([fiche], schema_rex)
    assert note["feuilles"] == 33, note["feuilles"]
    assert 0.0 < note["score"] <= 1.0


def test_remplissage_attrape_le_prompt_qui_se_vide(schema_rex):
    """
    Une fiche entièrement vide est parfaitement conforme au schéma : sans cette
    composante, une régression de ce genre serait notée sans défaut.
    """
    vide = {section: {champ: "" for champ in noeud["properties"]}
            for section, noeud in schema_rex["properties"].items()}
    assert eval_rex.noter_remplissage([vide], schema_rex)["score"] == 0.0


def test_feuilles_du_schema(schema_rex):
    chemins = eval_rex.feuilles_du_schema(schema_rex)
    assert len(chemins) == 33
    assert "Presentation/Titre" in chemins
    assert all("/" in c for c in chemins)


# --- Composite ---------------------------------------------------------------


def test_composite_refuse_une_composante_manquante():
    """
    Un score amputé ressemble à une régression alors qu'il ne mesure pas la même
    chose : mieux vaut ne rien rendre.
    """
    note = eval_rex.noter_decoupage(PARFAIT, VERITE)
    assert eval_rex.score_composite(note, None, None) is None
    assert eval_rex.score_composite(note, {"score": 1.0}, None) is None


def test_composite_ponderations():
    note = eval_rex.noter_decoupage(PARFAIT, VERITE)
    assert eval_rex.score_composite(note, {"score": 1.0}, {"score": 1.0}) == 100.0
    partiel = eval_rex.score_composite(note, {"score": 0.0}, {"score": 0.0})
    assert partiel == pytest.approx(45.0)
    assert sum(eval_rex.POIDS.values()) == pytest.approx(1.0)


# --- Journal -----------------------------------------------------------------


def test_ligne_journal_ne_fuit_aucun_contenu():
    """
    Le journal est versionné : il ne doit contenir que des nombres et des
    empreintes, jamais un titre de projet réel.
    """
    note = eval_rex.noter_decoupage(PARFAIT, VERITE)
    ligne = eval_rex.ligne_journal(note, {"prompt_sha256": "a" * 64,
                                          "modele_servi": "mistral-small-latest"},
                                   {"prompt_tokens": 12000, "cached_tokens": 0},
                                   VERITE)
    texte = json.dumps(ligne, ensure_ascii=False)
    for titre in ("Marais de Villiers", "Étang de Bracieux"):
        assert titre not in texte
    assert ligne["metrique"] == eval_rex.METRIQUE_DECOUPAGE
    assert ligne["couverture_verite"] == {"fiches": 2, "champs": 0}


def test_ligne_journal_nomme_la_metrique_complete():
    note = eval_rex.noter_decoupage(PARFAIT, VERITE)
    ligne = eval_rex.ligne_journal(note, {}, {}, VERITE,
                                   conformite_={"score": 1.0, "comptes": {"conforme": 2}},
                                   remplissage={"score": 0.7, "feuilles": 33},
                                   composite=93.5)
    assert ligne["metrique"] == eval_rex.METRIQUE_COMPLETE
    assert ligne["score"] == 93.5
    assert ligne["composantes"]["remplissage"] == 0.7


def test_notation_deterministe():
    """Deux notations de la même entrée doivent rendre exactement la même note."""
    a = eval_rex.noter_decoupage(PARFAIT, VERITE)
    b = eval_rex.noter_decoupage(PARFAIT, VERITE)
    assert a == b


# --- La vérité terrain versionnée -------------------------------------------


def test_verite_reelle_coherente():
    verite = json.loads((eval_rex.VERITE).read_text(encoding="utf-8"))
    pages = eval_rex.pages_hors_projet(verite)
    attendues = set()
    for fiche in verite["fiches_attendues"]:
        attendues |= set(range(fiche["page_debut"], fiche["page_fin"] + 1))
    assert not (pages & attendues), "une page ne peut être à la fois dans et hors projet"
    assert pages | attendues == set(range(1, verite["nb_pages"] + 1)), \
        "chaque page doit être classée exactement une fois"
