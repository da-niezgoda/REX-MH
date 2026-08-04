"""
Aplatissement, export Excel et libellés — hors ligne, sans client ni base.

Ces fonctions d'`app.py` sont sans `st.*` et se testent donc directement. Deux
cas verrouillent des défauts connus plutôt que de les reproduire : la collision
de noms de feuille et le repli de titre en minuscules.
"""
import io
import json
import zipfile

import pytest

import app
import conformite
from fabrique import fiche_conforme, fiche_de_test


@pytest.fixture(scope="module")
def fiche(schema_rex):
    f = fiche_de_test(schema_rex)
    f.update({"_project_title": "Marais de Villiers", "_page_debut": 10,
              "_page_fin": 14, "_segment_index": 0,
              "_model_ocr": "mistral-ocr-4-0",
              "_model_segmentation": "mistral-small-2506",
              "_model_extraction": "mistral-medium-2508",
              "_prompt_hash": "abcdef0123456789",
              "_validation_status": "corrige",
              "_validation_resume": "recalages : canonique ×1"})
    return f


# --- L'invariant de collision -----------------------------------------------


def test_pas_de_collision_de_noms_de_feuille(schema_rex):
    """
    `flatten_project_data` aplatit sur le nom de feuille NU. Deux sections
    partageant un nom de champ s'écraseraient donc, et une colonne disparaîtrait
    de l'Excel du client. Vrai aujourd'hui ; ce test le maintient vrai.
    """
    assert app.verifier_unicite_des_feuilles(schema_rex) == {}


def test_le_garde_fou_de_collision_fonctionne():
    """Un schéma jouet à doublon doit bien être signalé."""
    jouet = {"type": "object", "properties": {
        "A": {"type": "object", "properties": {"titre": {"type": "string"}}},
        "B": {"type": "object", "properties": {"titre": {"type": "string"}}},
    }}
    assert app.verifier_unicite_des_feuilles(jouet) == {"titre": ["A", "B"]}


def test_sections_couvrent_le_schema(schema_rex):
    """
    `SECTIONS` est écrit à la main : une section ajoutée au schéma et oubliée ici
    disparaîtrait silencieusement de l'export.
    """
    assert set(app.SECTIONS) == set(schema_rex["properties"])


# --- Aplatissement -----------------------------------------------------------


def test_listes_jointes_dans_toutes_les_sections(fiche):
    """Le défaut client : une liste hors Enjeux sortait avec ses crochets."""
    plat = app.flatten_project_data(fiche)
    for champ in ("enjeux", "type_valorisation"):
        assert isinstance(plat[champ], str)
        assert "," in plat[champ] and "[" not in plat[champ]


def test_metadonnees_completes_dans_l_export(fiche):
    plat = app.flatten_project_data(fiche)
    assert len(app.META_EXPORT) == 10
    for cle in app.META_EXPORT:
        assert cle in plat, cle
    assert plat["_validation_status"] == "corrige"


def test_resume_derive_du_json_au_rechargement(schema_rex, index_conformite):
    """
    Rechargée depuis la base, une fiche porte le verdict brut : le résumé
    français est dérivé à l'aplatissement, pour que `store.py` reste une feuille.
    """
    brute = fiche_conforme(schema_rex)
    brute["Presentation"]["Région"] = "Terre du Milieu"
    _, rapport = conformite.conformer(brute, index_conformite)
    recharge = dict(brute)
    recharge["_validation_status"] = rapport["statut"]
    recharge["_validation_errors_json"] = json.dumps(rapport, ensure_ascii=False)
    plat = app.flatten_project_data(recharge)
    assert "Presentation/Région" in plat["_validation_resume"]


# --- Classeur ----------------------------------------------------------------


def test_verdict_dans_le_classeur(fiche):
    octets = app.create_excel_download([fiche])
    assert octets[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(octets)) as zf:
        chaines = zf.read("xl/sharedStrings.xml").decode("utf-8")
    assert "_validation_status" in chaines
    assert "_validation_resume" in chaines


def test_aucune_liste_python_dans_le_classeur(fiche):
    octets = app.create_excel_download([fiche])
    with zipfile.ZipFile(io.BytesIO(octets)) as zf:
        chaines = zf.read("xl/sharedStrings.xml").decode("utf-8")
    premiere = fiche["Valorisation"]["type_valorisation"][0]
    assert f"['{premiere}'" not in chaines


def test_nom_export_insensible_a_la_casse():
    assert app._nom_export("IFD_FICJOINT.PDF") == "IFD_FICJOINT_REX_export.xlsx"
    assert app._nom_export("recueil.pdf") == "recueil_REX_export.xlsx"
    assert app._nom_export(None) == "export_REX_export.xlsx"


# --- Libellé de fiche --------------------------------------------------------


def test_titre_de_fiche_ordre_de_priorite():
    assert app._titre_de_fiche({"_project_title": "Y",
                                "Presentation": {"Titre": "X"}}, 0) == "Y"
    assert app._titre_de_fiche({"Presentation": {"Titre": "X"}}, 0) == "X"
    assert app._titre_de_fiche({}, 0) == "Projet 1"


def test_titre_minuscule_non_accepte():
    """
    Verrouille le correctif, sans reproduire le bug : `presentation` / `titre` en
    minuscules n'ont JAMAIS existé dans le schéma, donc les accepter serait
    accréditer une forme fausse.
    """
    assert app._titre_de_fiche({"presentation": {"titre": "X"}}, 0) == "Projet 1"


# --- Rendu HTML --------------------------------------------------------------


def test_toutes_les_sections_se_rendent(schema_rex):
    """
    `format_expanded_data` masque une section dont tous les champs sont vides
    (`if value and value != ""`). Avec une fiche « minimale », 5 des 10 blocs
    n'étaient donc jamais rendus — donc jamais vérifiés échappés. On remplit tout.
    """
    fiche = fiche_conforme(schema_rex, taille_tableau=2)
    remplissage = {
        "Presentation/Titre": "Marais de Villiers",
        "Presentation/Bassin": "Rhin-Meuse",
        "Presentation/Localisation": "Meurthe-et-Moselle",
        "Presentation/Adresse précise": "3 rue de l'Étang",
        "Presentation/Nom de l'organisme": "OiEau & Cie",
        "Objectif/objectifs": "Restaurer <la> tourbière",
        "Description/resume": "Un résumé.\n\nEn deux paragraphes.",
        "Description/publication_recueil": "2006",
        "Contexte/autres": "Arrêté préfectoral <de> biotope",
        "Travaux/surface_travaux": "21 ha",
        "Documents/pages_extraire": "3-5",
        "Documents/recueil_complet": "https://example.org/recueil",
        "Valorisation/url": "https://example.org/rex",
        "Valorisation/texte_lien": "Fiche en ligne",
        "Valorisation/prix_recompense": "Prix Ramsar",
        "Typologie/type_genie_ecologique_autre": "Suppression <de> barrage",
        "Directives/reference_masse_eau": "FRGR1234",
    }
    for chemin, valeur in remplissage.items():
        section, champ = chemin.split("/", 1)
        if champ in (schema_rex["properties"][section]["properties"]):
            fiche[section][champ] = valeur

    rendu = app.format_expanded_data(fiche)
    for titre in ("Objectif", "Résumé", "Autres", "Surface des travaux",
                  "Pages à extraire", "Recueil complet"):
        assert titre in rendu, f"bloc « {titre} » absent du rendu"
    # Tout ce qui est interpolé reste échappé, y compris dans les blocs qui
    # n'étaient jamais exercés.
    assert "<la>" not in rendu and "&lt;la&gt;" in rendu
    assert "<de>" not in rendu and "&lt;de&gt;" in rendu
    assert "&amp; Cie" in rendu


def test_fiche_vide_ne_rend_rien():
    assert app.format_expanded_data({}) == "Aucune donnée disponible"
    assert app.format_expanded_data(None) == "Aucune donnée disponible"


def test_enjeux_jointes_par_un_seul_chemin(schema_rex):
    """
    La branche Enjeux pré-joignait sa liste avant de la passer à `_e`, qui joint
    déjà : deux chemins de jointure pour un même besoin. Un seul désormais.
    """
    fiche = fiche_conforme(schema_rex, taille_tableau=2)
    attendu = ", ".join(fiche["Enjeux"]["enjeux"])
    rendu = app.format_expanded_data(fiche)
    assert attendu in rendu
