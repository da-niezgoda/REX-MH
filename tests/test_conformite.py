"""
Normalisation et validation — hors ligne, sans base, sans streamlit.

Deux balayages portent l'essentiel de la valeur : `test_variantes_reviennent` et
`test_aucun_recalage_croise`. Ensemble ils exercent les 204 valeurs
d'énumération du schéma contre une dizaine de mutations chacune, en quelques
millisecondes, et c'est ce qui rend défendable l'absence de distance d'édition.
"""
import json
from pathlib import Path

import jsonschema
import pytest

import conformite
from fabrique import chemins_enumeres, fiche_conforme, variantes_proches

RACINE = Path(__file__).resolve().parent.parent

# Alias volontairement en attente : « Site Natura 2000 » n'existe pas encore dans
# Contexte.contexte. La tâche 5 ajoutera la valeur et retirera ces lignes. Toute
# AUTRE anomalie d'index doit faire échouer la suite — c'est ce que garantit la
# comparaison à cette liste blanche, plutôt qu'un simple « on tolère les
# problèmes ».
EN_ATTENTE_TACHE_5 = {
    "Contexte/contexte : l'alias « Natura 2000 » vise « Site Natura 2000 », "
    "qui n'est pas dans l'énumération",
    "Contexte/contexte : l'alias « Zone Natura 2000 » vise « Site Natura 2000 », "
    "qui n'est pas dans l'énumération",
}


@pytest.fixture(scope="module")
def vocabulaire():
    return json.loads((RACINE / "vocabulary.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def index(schema_rex, vocabulaire):
    idx, _ = conformite.construire_index(schema_rex, vocabulaire)
    return idx


@pytest.fixture(scope="module")
def enums(schema_rex):
    return chemins_enumeres(schema_rex)


# --- Canonicalisation --------------------------------------------------------


def test_canonisation_idempotente(enums):
    zoo = [" Espace insécable ", "soft­hyphen", "Œuvre", "ﬁchier",
           "ＦＵＬＬＷＩＤＴＨ", "l’eau", "l'eau", "N/A", ""]
    for valeur in [v for liste in enums.values() for v in liste] + zoo:
        une = conformite.canoniser(valeur)
        assert conformite.canoniser(une) == une, valeur


def test_canonisation_resout_les_pieges():
    """Les paires que les énumérations métier contiennent réellement."""
    paires = [
        ("Conservation d'espèces patrimoniales", "Conservation despèces patrimoniales"),
        ("Auvergne-Rhône-Alpes", "AUVERGNE-RHONE-ALPES"),
        ("Régions d'étangs", "Régions détangs"),
        ("Saint-Pierre-et-Miquelon", "SAINT PIERRE ET MIQUELON"),
        ("Élévation du niveau de la mer", "Elévation du niveau de la mer"),
        ("Réouverture d'embouchure", "Réouverture dembouchure"),
    ]
    for a, b in paires:
        assert conformite.canoniser(a) == conformite.canoniser(b), (a, b)


def test_index_sans_ambiguite(index):
    """
    Aucun tier ne confond deux valeurs d'une même énumération.

    Recalculé sur le schéma COURANT : si la tâche 5 introduit une paire ambiguë,
    ce test tombe, et le correctif est une ligne de `tiers_desactives`.
    """
    assert conformite.verifier_injectivite(index) == []


def test_problemes_d_index_tous_declares(schema_rex, vocabulaire):
    _, problemes = conformite.construire_index(schema_rex, vocabulaire)
    assert set(problemes) == EN_ATTENTE_TACHE_5, set(problemes) ^ EN_ATTENTE_TACHE_5


# --- Les deux balayages ------------------------------------------------------


def test_variantes_reviennent_a_leur_valeur(index, enums):
    """Rappel : chaque mutation mécanique se recale sur sa propre valeur."""
    verifiees = 0
    for chemin, valeurs in enums.items():
        entree = index["champs"][chemin.removesuffix("[]")]
        for valeur in valeurs:
            for variante in variantes_proches(valeur):
                obtenu, _ = conformite.resoudre(variante, entree)
                assert obtenu == valeur, (chemin, variante, obtenu, valeur)
                verifiees += 1
    # 1 367 sur le schéma courant. Le seuil garde une marge : il n'est là que
    # pour signaler un balayage devenu vide par accident (un `enums` mal filtré),
    # pas pour figer un décompte que la tâche 5 fera bouger.
    assert verifiees > 1000, f"balayage trop maigre : {verifiees}"


def test_aucun_recalage_croise(index, enums):
    """
    Précision : aucune mutation de A ne se résout en une autre valeur B.

    C'est le garde-fou anti-corruption. Une distance d'édition le ferait tomber
    immédiatement sur `type_genie_ecologique`, qui contient « Fauche »,
    « Fenaison et pâture » et « Pâturage ».
    """
    for chemin, valeurs in enums.items():
        entree = index["champs"][chemin.removesuffix("[]")]
        for valeur in valeurs:
            for variante in variantes_proches(valeur):
                obtenu, _ = conformite.resoudre(variante, entree)
                assert obtenu in (valeur, variante), (chemin, variante, obtenu)


def test_valeur_hors_vocabulaire_laissee_intacte(index):
    """
    La frontière exacte : ce qui n'est qu'une variante d'orthographe se recale,
    ce qui est un autre mot ne se recale PAS et ressort signalé.
    """
    entree = index["champs"]["Contexte/contexte"]
    for inventee in ("Réserve martienne", "Site classifié", "Parc National du futur",
                     "Réserve de Chasse et de Pêche"):
        obtenu, regle = conformite.resoudre(inventee, entree)
        assert obtenu == inventee and regle is None, (inventee, obtenu, regle)
    # « Site classe » n'est que « Site classé » sans accent : lui doit se recaler.
    assert conformite.resoudre("Site classe", entree)[0] == "Site classé"


def test_alias_de_ponctuation(index):
    """« NA » et « N/A » ne se canonisent pas pareil : il faut un alias."""
    entree = index["champs"]["Directives/milieux_masse_eau_dce"]
    assert conformite.canoniser("NA") != conformite.canoniser("N/A")
    assert conformite.resoudre("NA", entree) == ("N/A", "alias")
    assert conformite.resoudre("Sans objet", entree) == ("N/A", "alias")


def test_code_et_libelle_ramsar(index):
    """Le défaut Ramsar annoncé dans plan.md, résolu sans le moindre alias."""
    entree = index["champs"]["Typologie/type_milieu_ramsar"]
    complete = next(v for v in entree["enum"] if v.startswith("U - "))
    assert conformite.resoudre("U", entree) == (complete, "code_ramsar")
    libelle = complete.split(" - ", 1)[1]
    assert conformite.resoudre(libelle, entree) == (complete, "libelle_ramsar")
    # Singulier sans code : le tier « pluriel » doit encore y arriver.
    singulier = libelle.replace("Tourbières", "Tourbière").replace("boisées", "boisée")
    if singulier != libelle:
        assert conformite.resoudre(singulier, entree)[0] == complete


# --- Listes ------------------------------------------------------------------


def test_dedoublonnage_ordre_preserve(index, schema_rex):
    enum = schema_rex["properties"]["Valorisation"]["properties"][
        "type_valorisation"]["items"]["enum"]
    a, b = enum[0], enum[1]
    fiche = fiche_conforme(schema_rex)
    fiche["Valorisation"]["type_valorisation"] = [a, "", a.upper(), b]
    obtenue, rapport = conformite.conformer(fiche, index)
    assert obtenue["Valorisation"]["type_valorisation"] == [a, b]
    regles = [c["regle"] for c in rapport["corrections"]]
    assert "vide_supprime" in regles and "doublon_supprime" in regles


def test_liste_entierement_vide_devient_vide(index, schema_rex):
    fiche = fiche_conforme(schema_rex)
    fiche["Enjeux"]["enjeux"] = ["", "  "]
    obtenue, rapport = conformite.conformer(fiche, index)
    assert obtenue["Enjeux"]["enjeux"] == []
    assert rapport["statut"] == "corrige", rapport["erreurs"]


# --- Champs à motif ----------------------------------------------------------


def test_espaces_normalises_sur_les_motifs(index, schema_rex):
    fiche = fiche_conforme(schema_rex)
    fiche["Description"]["publication_recueil"] = " 2006 "
    fiche["Valorisation"]["url"] = "https://example.org/rex "
    obtenue, rapport = conformite.conformer(fiche, index)
    assert obtenue["Description"]["publication_recueil"] == "2006"
    assert obtenue["Valorisation"]["url"] == "https://example.org/rex"
    assert rapport["statut"] == "corrige", rapport["erreurs"]


def test_paragraphes_du_texte_libre_preserves(index, schema_rex):
    """
    `Description.resume` est un récit de plusieurs paragraphes. Écraser ses
    « \\n\\n » détruisait la mise en forme d'un texte que le client lit, et
    rendait au passage CHAQUE fiche « corrigée » — donc la barre « zéro recalage
    = propre » inatteignable. Constaté sur un run réel.
    """
    recit = "Premier paragraphe.\n\nDeuxième paragraphe.\n\nTroisième."
    fiche = fiche_conforme(schema_rex)
    fiche["Description"]["resume"] = recit
    obtenue, rapport = conformite.conformer(fiche, index)
    assert obtenue["Description"]["resume"] == recit
    assert rapport["statut"] == "conforme", rapport["corrections"]


def test_texte_libre_debarrasse_de_ses_bords(index, schema_rex):
    """Les espaces de bord partent quand même : inutiles et invisibles."""
    fiche = fiche_conforme(schema_rex)
    fiche["Objectif"]["objectifs"] = "  Restaurer la tourbière.\n\nPuis suivre.  "
    obtenue, _ = conformite.conformer(fiche, index)
    assert obtenue["Objectif"]["objectifs"] == "Restaurer la tourbière.\n\nPuis suivre."


def test_enum_et_motifs_restent_sur_une_ligne(index, schema_rex):
    """Une valeur d'énumération ou à motif est un jeton : tout est écrasé."""
    fiche = fiche_conforme(schema_rex)
    fiche["Description"]["publication_recueil"] = "\n 2006 \n"
    fiche["Contexte"]["contexte"] = "  Site\n classé  "
    obtenue, _ = conformite.conformer(fiche, index)
    assert obtenue["Description"]["publication_recueil"] == "2006"
    assert obtenue["Contexte"]["contexte"] == "Site classé"


def test_dates_intactes_en_tache_3(index, schema_rex):
    """
    Contrat exécutable de la frontière tâche 3 / tâche 5.

    Réduire « 01/01/1991 » à « 1991 » maintenant rendrait CHAQUE fiche
    « corrigé » alors que le schéma admet encore JJ/MM/AAAA, ce qui mettrait hors
    d'atteinte la barre « zéro recalage = propre ». La tâche 5 changera le motif,
    la description, le prompt et la règle dans le même commit.
    """
    fiche = fiche_conforme(schema_rex)
    fiche["Enjeux"]["date_debut"] = "01/01/1991"
    obtenue, rapport = conformite.conformer(fiche, index)
    assert obtenue["Enjeux"]["date_debut"] == "01/01/1991"
    assert conformite.compter_recalages(rapport) == 0, rapport["corrections"]


def test_publication_recueil_peut_etre_vide(index, schema_rex):
    """
    Ancrage de la correction : le modèle doit pouvoir LAISSER publication_recueil
    vide quand aucune date de publication n'est explicite dans les pages — sinon il
    la devine (mesuré : 8/29 fiches divergentes, 2001→2013, sur un document unique).
    Le motif `^[0-9]{4}$`, requis et strict, l'INTERDISAIT et forçait une valeur.
    Relâché en `^([0-9]{4})?$` : vide est désormais conforme, sans recalage.
    """
    fiche = fiche_conforme(schema_rex)
    fiche["Description"]["publication_recueil"] = ""
    obtenue, rapport = conformite.conformer(fiche, index)
    assert rapport["statut"] == "conforme", rapport
    assert obtenue["Description"]["publication_recueil"] == ""
    assert conformite.compter_recalages(rapport) == 0, rapport["corrections"]


# --- Verdicts ----------------------------------------------------------------


def test_fiche_propre_est_conforme(index, schema_rex):
    obtenue, rapport = conformite.conformer(fiche_conforme(schema_rex), index)
    assert rapport["statut"] == "conforme", rapport
    assert rapport["corrections"] == [] and rapport["erreurs"] == []
    assert conformite.resumer(rapport) == "conforme"


def test_precedence_des_verdicts(index, schema_rex):
    """Une erreur résiduelle domine un recalage réussi."""
    fiche = fiche_conforme(schema_rex)
    fiche["Contexte"]["contexte"] = "site classe"          # recalable
    fiche["Presentation"]["Région"] = "Terre du Milieu"    # irrécupérable
    _, rapport = conformite.conformer(fiche, index)
    assert rapport["statut"] == "non_conforme"
    assert rapport["corrections"] and rapport["erreurs"]
    assert rapport["erreurs"][0]["chemin"] == "Presentation/Région"
    assert rapport["erreurs"][0]["validateur"] == "enum"
    assert rapport["erreurs"][0]["message"] == "valeur hors énumération contrôlée"


def test_message_derreur_en_francais(index, schema_rex):
    """L'interface est en français ; le message anglais du SDK reste au débogage."""
    fiche = fiche_conforme(schema_rex)
    fiche["Presentation"]["Région"] = "Terre du Milieu"
    _, rapport = conformite.conformer(fiche, index)
    erreur = rapport["erreurs"][0]
    assert "is not one of" not in erreur["message"]
    assert "is not one of" in erreur["message_jsonschema"]


def test_empreintes_dans_le_rapport(index, schema_rex):
    """Sans elles, un verdict est injugeable après une réécriture du vocabulaire."""
    _, rapport = conformite.conformer(fiche_conforme(schema_rex), index)
    assert rapport["schema_sha256"] and rapport["vocabulaire_sha256"]
    assert rapport["version"] == conformite.VERSION_RAPPORT


# --- Propriétés de la passe --------------------------------------------------


def test_conformer_ne_mute_pas_son_argument(index, schema_rex):
    fiche = fiche_conforme(schema_rex)
    fiche["Contexte"]["contexte"] = "site classe"
    avant = json.dumps(fiche, sort_keys=True, ensure_ascii=False)
    conformite.conformer(fiche, index)
    assert json.dumps(fiche, sort_keys=True, ensure_ascii=False) == avant


def test_normalisation_idempotente(index, schema_rex):
    fiche = fiche_conforme(schema_rex)
    fiche["Contexte"]["contexte"] = "  SITE CLASSE  "
    fiche["Enjeux"]["enjeux"] = [fiche["Enjeux"]["enjeux"][0], ""]
    une, premier = conformite.conformer(fiche, index)
    deux, second = conformite.conformer(une, index)
    assert premier["corrections"], "le premier passage doit corriger quelque chose"
    assert second["corrections"] == [], second["corrections"]
    assert deux == une


def test_journal_est_reversible(index, schema_rex):
    """
    Le journal porte `avant` et `apres`, donc la sortie brute du modèle reste
    reconstituable — c'est ce qui autorise à ne stocker que la version normalisée.
    """
    fiche = fiche_conforme(schema_rex)
    fiche["Contexte"]["contexte"] = "site classe"
    fiche["Description"]["publication_recueil"] = " 2006 "
    obtenue, rapport = conformite.conformer(fiche, index)
    rejouee = json.loads(json.dumps(obtenue, ensure_ascii=False))
    for correction in reversed(rapport["corrections"]):
        section, champ = correction["chemin"].split("/", 1)
        assert "[" not in champ, "ce cas ne porte pas sur une liste"
        rejouee[section][champ] = correction["avant"]
    assert rejouee == fiche


def test_cles_techniques_preservees(index, schema_rex):
    """
    Les clés « _ » traversent la passe sans être validées : la racine du schéma a
    additionalProperties: false, donc les valider ferait échouer chaque fiche.
    """
    fiche = fiche_conforme(schema_rex)
    fiche["_project_title"] = "Un titre"
    fiche["_segment_index"] = 3
    obtenue, rapport = conformite.conformer(fiche, index)
    assert obtenue["_project_title"] == "Un titre" and obtenue["_segment_index"] == 3
    assert rapport["statut"] == "conforme", rapport["erreurs"]


def test_section_absente_est_signalee_pas_inventee(index, schema_rex):
    fiche = fiche_conforme(schema_rex)
    del fiche["Travaux"]
    obtenue, rapport = conformite.conformer(fiche, index)
    assert "Travaux" not in obtenue, "la couche ne doit rien inventer"
    assert rapport["statut"] == "non_conforme"
    assert any(e["validateur"] == "required" for e in rapport["erreurs"])


def test_resume_lisible(index, schema_rex):
    fiche = fiche_conforme(schema_rex)
    fiche["Presentation"]["Région"] = "Terre du Milieu"
    fiche["Contexte"]["contexte"] = "site classe"
    _, rapport = conformite.conformer(fiche, index)
    resume = conformite.resumer(rapport)
    assert "Presentation/Région" in resume and "recalages" in resume
    assert conformite.resumer_json(json.dumps(rapport)) == resume
    assert conformite.resumer_json(None) == "" and conformite.resumer_json("{{") == ""


# --- Le schéma lui-même ------------------------------------------------------


def test_aucun_exemple_illegal_dans_le_schema(schema_rex):
    """
    Chaque `examples` doit valider contre son propre sous-schéma.

    Le schéma est injecté VERBATIM dans `REXPrompt.md` : un exemple illégal
    montre au modèle une valeur que le mode strict refusera ensuite. C'était le
    cas de `Enjeux.enjeux`, dont l'exemple portait « Conservation d'espèces
    patrimoniales » avec l'apostrophe que son énumération n'admet pas.
    """
    fautifs = []

    def parcourir(noeud, chemin):
        if not isinstance(noeud, dict):
            return
        exemples = noeud.get("examples")
        if exemples is not None:
            sans = {c: v for c, v in noeud.items() if c != "examples"}
            for i, exemple in enumerate(exemples):
                for erreur in jsonschema.Draft7Validator(sans).iter_errors(exemple):
                    fautifs.append(f"{chemin}/examples[{i}] : {erreur.message[:120]}")
        for nom, sous in (noeud.get("properties") or {}).items():
            parcourir(sous, f"{chemin}/{nom}")
        if isinstance(noeud.get("items"), dict):
            parcourir(noeud["items"], f"{chemin}[]")

    parcourir(schema_rex, "")
    assert fautifs == [], "\n".join(fautifs)
