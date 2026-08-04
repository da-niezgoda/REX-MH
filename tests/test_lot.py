"""
Chemins de REPLI du mode par lot — base et faux client propres à ce module.

Le parcours d'intégration reçoit ses résultats « inline » (`job.outputs`). Le
repli par téléchargement de fichier est justement ce qui s'exécute quand l'inline
manque : n'en tester que le cas facile laisserait dans l'ombre le code prévu pour
quand ça se passe mal.

Module séparé pour une raison précise : le faux `batch.jobs.create` renvoie
toujours `batch-1`, donc deux soumissions dans une même base entrent en collision
sur la clé primaire de `batch_jobs`. Une base par module règle la question sans
fragiliser l'ordre des tests d'intégration.
"""
import json

import pytest

import app
import store
from fabrique import fiche_de_test
from faux import FauxClient
from test_integration import NB_FICHES, PDF, SEGMENTS, _charge_ocr


@pytest.fixture
def lot(db_neuve, contexte_app):
    """Un lot soumis, prêt à être récolté par le chemin de repli."""
    fiche = fiche_de_test(contexte_app.REXSchema)
    client = FauxClient(ocr=_charge_ocr(), segments=SEGMENTS,
                        fiche=lambda page: fiche)
    app.client_mistral = lambda: client
    res = app.parse_pdf_document(PDF, "repli.pdf", mode="economique")
    assert res["statut"] == "en_attente", res["statut"]
    client.batch.jobs.sorties = []          # rien en inline : on force le repli
    return client, res, fiche


def _ligne_ok(seq, fiche):
    return json.dumps({
        "custom_id": f"seg-{seq:03d}",
        "response": {"status_code": 200, "body": {
            "choices": [{"message": {"content": json.dumps(fiche)}}]}}})


def test_recolte_par_telechargement_du_fichier_de_sortie(lot):
    client, res, fiche = lot
    client.batch.jobs.output_file = "file-out"
    client.files.contenu = "\n".join(_ligne_ok(i, fiche) for i in range(NB_FICHES))

    bilan = app.actualiser_travail_par_lot(res["job_id"])
    assert "file-out" in client.files.telecharges, client.files.telecharges
    assert bilan["recolte"] is True
    assert (bilan["ok"], bilan["echecs"], bilan["manquants"]) == (NB_FICHES, 0, 0)
    # Le point de passage unique doit avoir tourné sur ce chemin aussi.
    assert bilan["conformite"]["corrige"] == NB_FICHES
    lignes = store.list_fiches(res["run_id"], status="ok")
    assert {l["validation_status"] for l in lignes} == {"corrige"}


def test_json_invalide_est_une_categorie_distincte(lot):
    """
    Une réponse qui n'est pas du JSON n'est pas un échec d'API : la distinguer
    permet de savoir s'il faut relancer ou corriger le prompt.
    """
    client, res, fiche = lot
    client.batch.jobs.output_file = "file-out"
    client.files.contenu = json.dumps({
        "custom_id": "seg-000",
        "response": {"status_code": 200, "body": {
            "choices": [{"message": {"content": "{ceci n'est pas du json"}}]}}})

    bilan = app.actualiser_travail_par_lot(res["job_id"])
    assert bilan["echecs"] == 1 and bilan["manquants"] == NB_FICHES - 1
    fiche_0 = next(f for f in store.list_fiches(res["run_id"]) if f["seq"] == 0)
    assert fiche_0["status"] == "echec"
    assert fiche_0["categorie"] == "json_invalide"
    assert fiche_0["validation_status"] is None, \
        "un échec ne doit pas porter de verdict de conformité"


def test_fichier_derreurs_illisible_n_emporte_pas_la_recolte(lot):
    """Le fichier d'erreurs est du bonus : illisible, il ne doit rien casser."""
    client, res, fiche = lot
    client.batch.jobs.output_file = "file-out"
    client.batch.jobs.error_file = "file-err"
    client.files.contenus = {"file-out": _ligne_ok(0, fiche),
                             "file-err": "ceci n'est pas du JSONL"}

    bilan = app.actualiser_travail_par_lot(res["job_id"])
    assert bilan["recolte"] is True
    assert bilan["ok"] == 1, "la sortie valide doit être récoltée malgré tout"
    assert client.files.telecharges == ["file-out", "file-err"]


def test_travail_non_terminal_ne_recolte_pas(lot):
    """Tant que le travail tourne, rien ne doit être écrit."""
    client, res, _ = lot
    client.batch.jobs.statut = "RUNNING"
    bilan = app.actualiser_travail_par_lot(res["job_id"])
    assert bilan == {"statut": "RUNNING", "recolte": False}
    assert store.list_fiches(res["run_id"], status="ok") == []
    assert len(store.open_batch_jobs()) == 1


def test_travail_annule_met_le_run_en_echec(lot):
    client, res, _ = lot
    client.batch.jobs.statut = "CANCELLED"
    bilan = app.actualiser_travail_par_lot(res["job_id"])
    assert bilan["recolte"] is False
    assert store.get_run(res["run_id"])["status"] == "echec"


def test_erreurs_de_niveau_travail_decrivent_le_run(lot):
    """
    Un JSONL mal formé ou un modèle inconnu est une erreur du TRAVAIL, pas d'une
    fiche : elle doit atterrir sur le run.
    """
    client, res, fiche = lot
    client.batch.jobs.output_file = "file-out"
    client.files.contenu = _ligne_ok(0, fiche)
    client.batch.jobs.erreurs = [
        type("E", (), {"message": "modèle inconnu", "count": 3})()]

    bilan = app.actualiser_travail_par_lot(res["job_id"])
    assert "modèle inconnu" in bilan["erreurs_travail"]
    assert "modèle inconnu" in (store.get_run(res["run_id"])["error"] or "")


# --- Chemins d'échec d'un traitement ----------------------------------------
#
# Ils vivent ici plutôt que dans test_integration.py parce qu'ils exigent une
# base à eux : marquer un run en échec ou en ouvrir un concurrent perturberait
# le parcours linéaire partagé.


def test_aucune_fiche_reperee_met_le_run_en_echec(db_neuve, contexte_app):
    """Un découpage vide n'est pas un succès silencieux."""
    vide = {"PagesHorsProjet": list(range(1, 19)), "Liste": []}
    client = FauxClient(ocr=_charge_ocr(), segments=vide,
                        fiche=lambda page: {})
    app.client_mistral = lambda: client
    res = app.parse_pdf_document(PDF, "vide.pdf")
    assert res["statut"] == "echec"
    assert res["projects"] == []
    assert any("Aucune fiche" in a for a in res["avertissements"])
    assert store.get_run(res["run_id"])["status"] == "echec"


def test_document_deja_en_cours_est_refuse(db_neuve, contexte_app):
    """
    Le garde-fou anti-double-facturation, vu depuis l'application : l'index
    partiel de `runs` lève une IntegrityError, que `parse_pdf_document` doit
    traduire en refus lisible plutôt qu'en trace.
    """
    fiche = fiche_de_test(contexte_app.REXSchema)
    client = FauxClient(ocr=_charge_ocr(), segments=SEGMENTS,
                        fiche=lambda page: fiche)
    app.client_mistral = lambda: client
    res = app.parse_pdf_document(PDF, "encours.pdf", mode="economique")
    assert res["statut"] == "en_attente"      # le run reste « en_cours »

    encore = app.parse_pdf_document(PDF, "encours.pdf")
    assert encore["statut"] == "echec"
    assert any("déjà en cours" in a for a in encore["avertissements"]), \
        encore["avertissements"]
    assert client.ocr.appels == 1, "le second essai ne doit rien relancer"


def test_erreur_inattendue_marque_le_run_et_remonte(db_neuve, contexte_app):
    """
    Un bug de notre code doit laisser une trace en base ET remonter : le taire
    donnerait un run « terminé » sans fiches.
    """
    client = FauxClient(ocr=_charge_ocr(), segments=SEGMENTS,
                        fiche=lambda page: fiche_de_test(contexte_app.REXSchema))
    app.client_mistral = lambda: client

    def exploser(*a, **kw):
        raise RuntimeError("panne simulée")

    reel, store.set_run_segmentation = store.set_run_segmentation, exploser
    try:
        with pytest.raises(RuntimeError, match="panne simulée"):
            app.parse_pdf_document(PDF, "panne.pdf")
    finally:
        store.set_run_segmentation = reel

    run = store.list_runs()[0]
    assert run["status"] == "echec"
    assert "panne simulée" in (store.get_run(run["id"])["error"] or "")
