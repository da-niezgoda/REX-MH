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


# --- Rendu natif du détail (blocs_de_fiche, pur et testable) -----------------
#
# `rendre_fiche` émet des st.* et n'est pas testable hors Streamlit ; toute la
# logique vit dans `blocs_de_fiche`, pure, qu'on vérifie ici. L'inversion clé de
# la tâche 4 : le rendu natif échappe tout seul, donc la DONNÉE garde le texte
# verbatim au lieu d'être pré-échappée à la main (_e/_url ont disparu).


def _textes_de(blocs):
    """(libellé, texte) de tous les champs de tous les blocs."""
    return [(lib, txt) for _, _, champs in blocs for lib, txt, _ in champs]


def test_toutes_les_sections_se_rendent(schema_rex):
    """Toute section non vide produit un bloc titré ; piloté par le schéma."""
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

    blocs = app.blocs_de_fiche(fiche)
    titres = {titre for _, titre, _ in blocs}
    for attendu in ("Objectif du maître d'ouvrage", "Description",
                    "Contexte réglementaire", "Période et envergure des travaux",
                    "Documents"):
        assert attendu in titres, f"bloc « {attendu} » absent"

    libelles = {lib for lib, _ in _textes_de(blocs)}
    for attendu in ("Objectifs", "Résumé", "Autres", "Surface des travaux",
                    "Pages à extraire", "Recueil complet"):
        assert attendu in libelles, f"libellé « {attendu} » absent"

    # Le texte reste VERBATIM : l'échappement est celui, natif, de st.markdown.
    textes = {txt for _, txt in _textes_de(blocs)}
    assert "Restaurer <la> tourbière" in textes
    assert "OiEau & Cie" in textes


def test_champ_url_marque_comme_lien(schema_rex):
    """Un champ d'URL http(s) est marqué « lien » ; le reste ne l'est pas."""
    fiche = fiche_conforme(schema_rex, taille_tableau=2)
    fiche["Documents"]["recueil_complet"] = "https://example.org/recueil"
    liens = {
        lib: est_lien
        for _, _, champs in app.blocs_de_fiche(fiche)
        for lib, _, est_lien in champs
    }
    assert liens.get("Recueil complet") is True


def test_libelle_champ_repli_sur_la_cle():
    """Un champ hors LIBELLES retombe sur une version « jolie » de sa clé."""
    assert app._libelle_champ("Presentation", "Titre") == "Titre"
    assert app._libelle_champ("Xxx", "type_genie_ecologique") == "Type genie ecologique"


def test_valeur_affichable_joint_les_listes():
    assert app._valeur_affichable(["a", "b", "c"]) == "a, b, c"
    assert app._valeur_affichable(["a", "", None, "b"]) == "a, b"
    assert app._valeur_affichable(None) == ""


def test_resume_tronque_a_500(schema_rex):
    """Le résumé long est coupé à 500 caractères + « … »."""
    fiche = fiche_conforme(schema_rex, taille_tableau=2)
    fiche["Description"]["resume"] = "x" * 800
    textes = {txt for _, _, champs in app.blocs_de_fiche(fiche) for _, txt, _ in champs}
    resume = next(t for t in textes if t.startswith("xxx"))
    assert len(resume) == 501 and resume.endswith("…")


def test_fiche_vide_ne_rend_rien():
    assert app.blocs_de_fiche({}) == []
    assert app.blocs_de_fiche(None) == []


def test_enjeux_jointes_par_un_seul_chemin(schema_rex):
    """Une liste (enjeux) est jointe par « , » — dans toutes les sections."""
    fiche = fiche_conforme(schema_rex, taille_tableau=2)
    attendu = ", ".join(fiche["Enjeux"]["enjeux"])
    textes = {txt for _, _, champs in app.blocs_de_fiche(fiche) for _, txt, _ in champs}
    assert attendu in textes
