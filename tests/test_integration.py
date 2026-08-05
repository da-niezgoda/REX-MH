"""
Intégration hors ligne : app.py + pipeline.py + store.py avec un faux client.

Aucun appel API. Couvre le chemin complet — OCR, cache OCR, segmentation,
extraction parallèle, persistance, rechargement, export Excel, mode par lot.

La séquence est volontairement gardée LINÉAIRE derrière une seule fixture de
portée module : les passages coûteux (deux traitements complets, une relance, une
soumission de lot puis sa récolte) tournent une fois, et chaque section devient
un test nommé qui échoue indépendamment.
"""
import io
import json
import zipfile

import jsonschema
import pytest

import app
import pipeline
import store
from fabrique import CONTEXTE_APPROXIMATIF, CONTEXTE_ATTENDU, fiche_de_test
from faux import JETONS_PROMPT, FauxClient
from mistralai.client.models import OCRResponse

NB_PAGES = 18
NB_SEGMENTS = 7
NB_FICHES = NB_SEGMENTS - 2   # deux segments sont volontairement invalides


def _charge_ocr():
    """Fausse réponse OCR, validée par les VRAIS modèles du SDK."""
    brut = {
        "model": "mistral-ocr-4-0",
        "usage_info": {"pages_processed": NB_PAGES, "doc_size_bytes": 500_000},
        "pages": [
            {
                "index": i,
                "markdown": f"# Fiche page {i + 1}\n\nRestauration de tourbière <alcaline>.",
                "images": [],
                "dimensions": {"dpi": 200, "height": 2339, "width": 1654},
                "header": f"Recueil — page {i + 1}",
                "confidence_scores": {"average_page_confidence_score": 0.97,
                                      "minimum_page_confidence_score": 0.81},
                "blocks": [
                    {"type": "title", "content": f"Fiche {i + 1}", "top_left_x": 10,
                     "top_left_y": 10, "bottom_right_x": 500, "bottom_right_y": 60},
                    {"type": "text", "content": "Corps de la fiche.", "top_left_x": 10,
                     "top_left_y": 70, "bottom_right_x": 500, "bottom_right_y": 400},
                ],
            }
            for i in range(NB_PAGES)
        ],
    }
    ocr = OCRResponse.model_validate(brut)
    assert ocr.pages[0].blocks[0].content == "Fiche 1", "les blocs doivent survivre"
    assert ocr.pages[0].confidence_scores.average_page_confidence_score == 0.97
    return ocr


SEGMENTS = {
    "PagesHorsProjet": [],
    "Liste": [
        {"PageDebut": 1 + i * 2, "PageFin": 2 + i * 2, "Titre": f"Projet {i}",
         "Motif": "site nommé et maître d'ouvrage identifiés"}
        for i in range(NB_SEGMENTS - 2)
    ] + [
        # Le bug historique : PageDebut == 0 était pris pour une clé absente.
        {"PageDebut": 0, "PageFin": 3, "Titre": "Segment à page zéro", "Motif": "x"},
        {"PageDebut": 90, "PageFin": 95, "Titre": "Segment hors document", "Motif": "x"},
    ],
}

PDF = b"%PDF-1.4 faux contenu de test"


class Parcours:
    """Résultats du parcours complet, pour que les tests n'aient qu'à affirmer."""


@pytest.fixture(scope="module")
def parcours(db_temporaire, contexte_app):
    p = Parcours()
    p.fiche = fiche_de_test(contexte_app.REXSchema)
    p.attendu_valorisation = ", ".join(p.fiche["Valorisation"]["type_valorisation"])
    p.attendu_enjeux = ", ".join(p.fiche["Enjeux"]["enjeux"])
    assert "," in p.attendu_valorisation, "il faut deux valeurs pour tester la jointure"

    ocr = _charge_ocr()

    def faux(**kw):
        return FauxClient(ocr=ocr, segments=SEGMENTS,
                          fiche=lambda page: p.fiche, **kw)

    # 1-2. Premier passage en mode rapide.
    p.etapes = []
    p.client = faux()
    app.client_mistral = lambda: p.client
    p.res = app.parse_pdf_document(
        PDF, "recueil-test.pdf",
        progress_callback=lambda avancement, message, **d: p.etapes.append(
            (round(avancement, 3), message, d.get("phase"))))

    # 3. Deuxième passage : l'OCR doit venir du cache.
    p.res2 = app.parse_pdf_document(PDF, "recueil-test.pdf")

    # 6. Un échec d'API isolé, puis la relance.
    p.client_defaillant = faux(echouer={5})
    app.client_mistral = lambda: p.client_defaillant
    p.res3 = app.parse_pdf_document(PDF, "recueil-test.pdf")
    p.echecs_api = [e for e in p.res3["failures"]
                    if e["categorie"] != "segment_invalide"]
    app.client_mistral = lambda: faux()          # l'API redevient saine
    p.bilan_relance = app.relancer_fiches(
        p.res3["document_id"], p.res3["run_id"], [e["index"] for e in p.echecs_api])

    # 7. Mode économique : soumission puis récolte.
    p.lot = faux()
    app.client_mistral = lambda: p.lot
    p.res4 = app.parse_pdf_document(PDF, "recueil-test.pdf", mode="economique")
    corps = {"choices": [{"message": {"content": json.dumps(p.fiche)}}]}
    p.lot.batch.jobs.sorties = [
        {"custom_id": "seg-000", "response": {"status_code": 200, "body": corps}},
        {"custom_id": "seg-001", "response": {"status_code": 500, "body": {}},
         "error": "serveur indisponible"},
        {"custom_id": "seg-002", "response": {"status_code": 200, "body": corps}},
    ]  # seg-003 et seg-004 manquent volontairement
    p.bilan_lot = app.actualiser_travail_par_lot("batch-1")

    # 8. Archive de l'historique.
    p.archive = store.export_bundle(include_ocr=True)
    p.rapport_archive = store.import_bundle(p.archive)
    return p


# === 1. Mode rapide, premier passage ========================================


def test_rapide_fiches_et_echecs_nommes(parcours):
    res = parcours.res
    assert res["statut"] == "partiel"
    assert len(res["projects"]) == NB_FICHES
    assert [e["categorie"] for e in res["failures"]] == ["segment_invalide"] * 2
    assert "page zéro" in res["failures"][0]["titre"].lower()
    assert parcours.client.ocr.appels == 1
    assert res["ocr_cache_hit"] is False


def test_rapide_ordre_document(parcours):
    assert [f["_segment_index"] for f in parcours.res["projects"]] == list(range(NB_FICHES))


def test_rapide_cle_de_cache_unique(parcours):
    """Une seule clé pour toutes les extractions, sinon rien ne peut être caché."""
    chat = parcours.client.chat
    cles = [c for m, c in zip(chat.appels, chat.cles) if m == pipeline.MODEL_EXTRACTION]
    assert len(set(cles)) == 1
    assert cles[0].startswith("rex-extraction-")
    assert parcours.res["usage"]["extraction"]["cached_tokens"] > 0


def test_rapide_pdf_televerse_supprime(parcours):
    """Il fuyait à chaque run avant la tâche 2."""
    assert parcours.client.files.supprimes == ["file-1"]


def test_rapide_progression_monotone(parcours):
    avancements = [e[0] for e in parcours.etapes]
    assert avancements == sorted(avancements)
    assert 0.0 <= min(avancements) and max(avancements) == 1.0
    phases = [e[2] for e in parcours.etapes if e[2]]
    assert phases[0] == "upload" and phases[-1] == "extraction"


# === 2. Persistance =========================================================


def test_persistance_du_run(parcours):
    run = store.get_run(parcours.res["run_id"])
    assert run["status"] == "partiel" and run["mode"] == "rapide"
    assert run["model_ocr"] == "mistral-ocr-4-0"
    assert run["model_extraction"] == "mistral-medium-2508"
    assert run["prompt_extraction_sha256"] and run["schema_rex_sha256"]
    # 5 extractions + 1 segmentation
    assert run["prompt_tokens"] == JETONS_PROMPT * (NB_FICHES + 1)
    assert len(store.list_fiches(parcours.res["run_id"])) == NB_SEGMENTS
    assert len(store.load_failures(parcours.res["run_id"])) == 2


def test_fiche_stockee_conforme_au_schema(parcours, contexte_app):
    stocke = json.loads(
        store.list_fiches(parcours.res["run_id"], status="ok")[0]["data_json"])
    assert not any(cle.startswith("_") for cle in stocke)
    jsonschema.Draft7Validator(contexte_app.REXSchema).validate(stocke)


# === 2 bis. Conformité, sur les deux chemins d'arrivée ======================


def test_verdict_stocke_en_mode_rapide(parcours):
    lignes = store.list_fiches(parcours.res["run_id"], status="ok")
    assert {l["validation_status"] for l in lignes} == {"corrige"}, \
        [l["validation_status"] for l in lignes]
    verdict = json.loads(lignes[0]["validation_errors_json"])
    assert verdict["corrections"][0]["chemin"] == "Contexte/contexte"
    assert verdict["corrections"][0]["apres"] == CONTEXTE_ATTENDU
    assert verdict["schema_sha256"], "sans empreinte, un verdict est injugeable"


def test_valeur_normalisee_est_celle_stockee(parcours):
    """La base ne doit pas contenir la valeur brute du modèle."""
    stocke = json.loads(
        store.list_fiches(parcours.res["run_id"], status="ok")[0]["data_json"])
    assert stocke["Contexte"]["contexte"] == CONTEXTE_ATTENDU
    assert stocke["Contexte"]["contexte"] != CONTEXTE_APPROXIMATIF


def test_valeur_normalisee_est_celle_affichee(parcours):
    """
    La liste en session doit porter la version normalisée, sinon l'écran et
    l'Excel montreraient une valeur que la base ne contient pas.
    """
    for projet in parcours.res["projects"]:
        assert projet["Contexte"]["contexte"] == CONTEXTE_ATTENDU
        assert projet["_validation_status"] == "corrige"
        assert projet["_validation_resume"]


def test_bilan_de_conformite_du_run(parcours):
    bilan = parcours.res["conformite"]
    assert bilan["corrige"] == NB_FICHES and bilan["non_conforme"] == 0
    assert bilan["recalages"] == NB_FICHES
    assert bilan["par_regle"]["canonique"] == NB_FICHES


def test_verdict_stocke_en_mode_par_lot(parcours):
    """Le mode économique passe par le même point unique."""
    lignes = store.list_fiches(parcours.res4["run_id"], status="ok")
    assert lignes and {l["validation_status"] for l in lignes} == {"corrige"}
    stocke = json.loads(lignes[0]["data_json"])
    assert stocke["Contexte"]["contexte"] == CONTEXTE_ATTENDU
    assert parcours.bilan_lot["conformite"]["corrige"] == 2


def test_relance_rafraichit_le_verdict(parcours):
    """Une fiche relancée doit porter un verdict décrivant SES données."""
    lignes = store.list_fiches(parcours.res3["run_id"], status="ok")
    assert lignes and all(l["validation_status"] == "corrige" for l in lignes)


# === 3. Cache OCR ===========================================================


def test_second_passage_sans_appel_ocr(parcours):
    assert parcours.res2["ocr_cache_hit"] is True
    assert parcours.client.ocr.appels == 1, "l'OCR a été rappelé alors qu'il était en cache"
    assert len(parcours.client.files.televerses) == 1, "le PDF a été retéléversé"
    assert parcours.res2["run_id"] != parcours.res["run_id"]


# === 4. Rechargement et export ==============================================


def test_rechargement(parcours):
    data = store.load_run_as_parsed_data(parcours.res["run_id"])
    assert data["filename"] == "recueil-test.pdf"
    assert len(data["projects"]) == NB_FICHES
    assert data["projects"][0]["_model_extraction"] == "mistral-medium-2508"


def test_aplatissement_joint_les_listes_partout(parcours):
    """Le défaut client : une liste hors section Enjeux sortait avec ses crochets."""
    data = store.load_run_as_parsed_data(parcours.res["run_id"])
    plat = app.flatten_project_data(data["projects"][0])
    assert plat["type_valorisation"] == parcours.attendu_valorisation
    assert plat["enjeux"] == parcours.attendu_enjeux
    assert plat["_model_extraction"] == "mistral-medium-2508"
    assert plat["_prompt_hash"]


def test_classeur_excel(parcours):
    data = store.load_run_as_parsed_data(parcours.res["run_id"])
    octets = app.create_excel_download(data["projects"])
    assert octets[:2] == b"PK" and len(octets) > 4000
    # Relu depuis l'archive xlsx : openpyxl n'est pas une dépendance du projet.
    with zipfile.ZipFile(io.BytesIO(octets)) as zf:
        chaines = zf.read("xl/sharedStrings.xml").decode("utf-8")
    assert parcours.attendu_valorisation in chaines, "valeurs jointes absentes"
    assert "_model_extraction" in chaines, "traçabilité absente du classeur"
    premiere = parcours.fiche["Valorisation"]["type_valorisation"][0]
    assert f"['{premiere}'" not in chaines, "une liste Python brute a atteint le classeur"


def test_nom_export_insensible_a_la_casse():
    assert app._nom_export("IFD_FICJOINT.PDF") == "IFD_FICJOINT_REX_export.xlsx"
    assert app._nom_export("recueil.pdf") == "recueil_REX_export.xlsx"


# === 5. Rendu natif : le texte reste inerte ================================
# `rendre_fiche` s'appuie sur l'échappement natif de st.markdown : la DONNÉE
# garde le texte verbatim, et c'est le rendu qui neutralise le HTML — plus de
# _e()/_url(). On vérifie donc la donnée, pas une chaîne HTML.


def test_le_texte_reste_verbatim(parcours):
    data = store.load_run_as_parsed_data(parcours.res["run_id"])
    textes = {txt for _, _, champs in app.blocs_de_fiche(data["projects"][0])
              for _, txt, _ in champs}
    assert "Restauration <tourbière> & marais" in textes


def test_url_non_http_jamais_rendue_en_lien():
    """Un « javascript: » n'est jamais marqué comme lien ; seul http(s) l'est."""
    malveillant = {"Presentation": {"Titre": "<script>alert(1)</script>"},
                   "Valorisation": {"url": "javascript:alert(1)",
                                    "type_valorisation": ["a", "b"]}}
    champs = {lib: (txt, est_lien)
              for _, _, cs in app.blocs_de_fiche(malveillant)
              for lib, txt, est_lien in cs}
    # <script> conservé tel quel (neutralisé au rendu, pas ici)
    assert champs["Titre"] == ("<script>alert(1)</script>", False)
    # javascript: présent en texte, mais PAS marqué comme lien
    assert champs[app._libelle_champ("Valorisation", "url")] == \
        ("javascript:alert(1)", False)
    # liste jointe, hors Enjeux
    assert champs[app._libelle_champ("Valorisation", "type_valorisation")][0] == "a, b"


# === 6. Relance des fiches en échec =========================================


def test_echec_api_isole(parcours):
    assert len(parcours.echecs_api) == 1
    assert parcours.echecs_api[0]["reessayable"] is True
    assert parcours.echecs_api[0]["categorie"] == "quota"


def test_relance_recupere_la_fiche_sans_nouvel_ocr(parcours):
    assert parcours.bilan_relance["relancees"] == 1
    assert parcours.bilan_relance["encore_en_echec"] == 0
    apres = store.load_run_as_parsed_data(parcours.res3["run_id"])
    assert len(apres["projects"]) == NB_FICHES


# === 7. Mode économique =====================================================


def test_lot_soumission(parcours):
    assert parcours.res4["statut"] == "en_attente"
    assert parcours.res4["job_id"] == "batch-1"
    assert parcours.lot.files.televerses[0]["purpose"] == "batch"
    lignes = parcours.lot.files.televerses[0]["file"]["content"].decode().splitlines()
    assert len(lignes) == NB_FICHES
    premiere = json.loads(lignes[0])
    assert premiere["custom_id"] == "seg-000"
    assert "model" not in premiere["body"], "le modèle est fixé au niveau du travail"
    assert premiere["body"]["response_format"]["json_schema"]["strict"] is True
    assert premiere["body"]["prompt_cache_key"].startswith("rex-extraction-")
    assert parcours.lot.batch.jobs.crees[0]["endpoint"] == "/v1/chat/completions"
    assert parcours.lot.batch.jobs.crees[0]["metadata"]["application"] == "rex-mh"


def test_lot_enregistre_avant_retour(parcours):
    """Une mort de processus entre create et l'écriture laisserait un job orphelin."""
    travail = store.get_batch_job("batch-1")
    assert travail is not None
    assert json.loads(travail["fiche_seq_map_json"])["seg-000"] == 0


def test_lot_recolte(parcours):
    bilan = parcours.bilan_lot
    assert bilan["recolte"] is True
    assert (bilan["ok"], bilan["echecs"], bilan["manquants"]) == (2, 1, 2)
    assert store.open_batch_jobs() == [], "un travail terminé sort de la liste ouverte"


def test_lot_custom_id_absent_est_un_echec(parcours):
    """
    Sans ce contrôle, un fichier de sortie tronqué ferait disparaître des fiches
    silencieusement. « segment_invalide » vient des deux segments refusés avant
    soumission, « lot » de la ligne en erreur, « absent_lot » des deux manquantes.
    """
    categories = {f["categorie"]
                  for f in store.list_fiches(parcours.res4["run_id"], status="echec")}
    assert categories == {"lot", "absent_lot", "segment_invalide"}


def test_lot_jsonl_supprime_apres_recolte(parcours):
    jsonl = next(f"file-{i + 1}" for i, kw in enumerate(parcours.lot.files.televerses)
                 if kw["purpose"] == "batch")
    assert jsonl in parcours.lot.files.supprimes, parcours.lot.files.supprimes


# === 8. Archive de l'historique =============================================


def test_archive_de_l_historique(parcours):
    assert parcours.rapport_archive["runs_ignores"] >= 4
    assert parcours.rapport_archive["documents_existants"] == 1
    stats = store.historique_stats()
    assert stats["documents"] == 1 and stats["runs"] >= 4
