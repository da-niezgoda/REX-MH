"""Vérification hors ligne de store.py — aucun appel API, aucun streamlit."""
import io
import json
import sqlite3
import zipfile

import pytest

import store

SHA = "a" * 64
PAYLOAD = json.dumps({"pages": [{"index": 0, "markdown": "# Fiche accentuée é"}]})
FICHE = {"Presentation": {"Titre": "Restauration <tourbière>"},
         "_project_title": "A", "_page_debut": 3, "_model_extraction": "x"}


def _document(sha=SHA, nom="recueil.pdf"):
    doc_id = store.get_or_create_document(sha, nom, size_bytes=1234)
    store.save_ocr_payload(doc_id, PAYLOAD, cle_ocr="k1", model="mistral-ocr-4-0",
                           pages_processed=18, avg_confidence=0.97, sdk_version="2.8.0")
    return doc_id


def _run_avec_fiches(doc_id, *, terminer=True):
    """Un run avec une fiche OK et une en échec, plus le cumul de jetons."""
    run_id, _ = store.start_run(doc_id, mode="rapide", prompt_extraction_sha256="p1",
                                schema_rex_sha256="s1")
    store.set_run_segmentation(run_id, json.dumps({"Liste": [{"Titre": "A"}]}),
                               model_segmentation="mistral-small-2506")
    store.set_run_models(run_id, model_ocr="mistral-ocr-4-0",
                         model_extraction="mistral-medium-2508")
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", page_debut=3,
                       page_fin=5, data=FICHE,
                       model_extraction="mistral-medium-2508", prompt_hash="p1",
                       usage={"prompt_tokens": 12000, "cached_tokens": 8800,
                              "completion_tokens": 900})
    store.upsert_fiche(run_id, doc_id, 1, status="echec", titre="B", page_debut=6,
                       page_fin=7, error="SDKError: 429", categorie="quota")
    store.add_run_usage(run_id, prompt_tokens=12000, cached_tokens=8800,
                        completion_tokens=900)
    store.add_run_usage(run_id, prompt_tokens=12000, cached_tokens=11500,
                        completion_tokens=800)
    if terminer:
        store.finish_run(run_id, status="partiel")
    return run_id


def _archive_complete():
    """Un document, un run rapide avec deux fiches, un run par lot terminé."""
    doc_id = _document()
    run_id = _run_avec_fiches(doc_id)
    run2, _ = store.start_run(doc_id, mode="economique")
    store.record_batch_job("job-abc", run_id=run2, document_id=doc_id,
                           endpoint="/v1/chat/completions", kind="extraction",
                           status="QUEUED", input_file_id="file-1",
                           fiche_seq_map={"seg-000": 0, "seg-001": 1})
    store.finish_run(run2, status="termine")
    return doc_id, run_id, store.export_bundle(include_ocr=True,
                                               mistralai_version="2.8.0")


# --- schéma, documents, cache OCR -------------------------------------------


def test_init_db(db_neuve):
    assert store.schema_version() == store.SCHEMA_VERSION
    assert store.list_documents() == []


def test_unicite_du_sha256(db_neuve):
    doc_id = _document()
    assert store.get_or_create_document(SHA, "autre-nom.pdf") == doc_id


def test_aller_retour_gzip_du_cache_ocr(db_neuve):
    doc_id = _document()
    assert store.get_ocr_payload(doc_id) == PAYLOAD
    assert store.get_ocr_payload(doc_id, cle_ocr="k1") == PAYLOAD


def test_cle_ocr_differente_est_un_defaut_de_cache(db_neuve):
    """Basculer include_blocks ne doit pas servir une charge périmée."""
    doc_id = _document()
    assert store.get_ocr_payload(doc_id, cle_ocr="autre") is None


def test_get_ocr_meta_ne_charge_pas_le_blob(db_neuve):
    doc_id = _document()
    store.set_document_pages(doc_id, 18)
    meta = store.get_ocr_meta(doc_id)
    assert "payload_gz" not in meta
    assert meta["payload_bytes"] == len(PAYLOAD.encode())


def test_invalidation_puis_reecriture(db_neuve):
    doc_id = _document()
    store.mark_ocr_payload_invalid(doc_id, "test")
    assert store.get_ocr_payload(doc_id) is None
    assert not store.has_ocr_payload(doc_id)
    store.save_ocr_payload(doc_id, PAYLOAD, cle_ocr="k1")
    assert store.has_ocr_payload(doc_id)


# --- runs et fiches ----------------------------------------------------------


def test_un_seul_run_en_cours_par_document(db_neuve):
    """L'index partiel est le vrai garde-fou contre la double facturation."""
    doc_id = _document()
    store.start_run(doc_id, mode="rapide")
    with pytest.raises(sqlite3.IntegrityError):
        store.start_run(doc_id, mode="rapide")


def test_upsert_fiche_est_idempotent(db_neuve):
    doc_id = _document()
    run_id = _run_avec_fiches(doc_id, terminer=False)
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A bis", page_debut=3,
                       page_fin=5, data=FICHE,
                       model_extraction="mistral-medium-2508", prompt_hash="p1")
    assert len(store.list_fiches(run_id)) == 2, "UNIQUE(run_id, seq) => upsert"
    assert store.list_fiches(run_id, status="ok")[0]["titre"] == "A bis"


def test_les_cles_soulignees_ne_vont_pas_dans_data_json(db_neuve):
    """`REX.schema.json` a additionalProperties: false à la racine."""
    doc_id = _document()
    run_id = _run_avec_fiches(doc_id)
    stocke = json.loads(store.list_fiches(run_id, status="ok")[0]["data_json"])
    assert not any(cle.startswith("_") for cle in stocke)


def test_cumul_incremental_des_jetons(db_neuve):
    doc_id = _document()
    run_id = _run_avec_fiches(doc_id)
    run = store.get_run(run_id)
    assert (run["prompt_tokens"], run["cached_tokens"]) == (24000, 20300)
    assert run["status"] == "partiel" and run["finished_at"]


def test_rechargement_reinjecte_les_cles_soulignees(db_neuve):
    doc_id = _document()
    run_id = _run_avec_fiches(doc_id)
    recharge = store.load_run_as_parsed_data(run_id)
    assert recharge["filename"] == "recueil.pdf"
    assert len(recharge["projects"]) == 1
    projet = recharge["projects"][0]
    assert projet["_project_title"] == "A" and projet["_page_debut"] == 3
    assert projet["_model_ocr"] == "mistral-ocr-4-0"
    assert projet["_model_extraction"] == "mistral-medium-2508"
    assert projet["Presentation"]["Titre"] == "Restauration <tourbière>"


def test_load_failures(db_neuve):
    doc_id = _document()
    run_id = _run_avec_fiches(doc_id)
    assert store.load_failures(run_id) == [
        {"index": 1, "titre": "B", "pages": (6, 7), "categorie": "quota",
         "error": "SDKError: 429"}
    ]


def test_compteurs_de_listes(db_neuve):
    doc_id = _document()
    _run_avec_fiches(doc_id)
    runs = store.list_runs(doc_id)
    assert len(runs) == 1 and runs[0]["nb_ok"] == 1 and runs[0]["nb_echec"] == 1
    assert store.list_documents()[0]["nb_runs"] == 1
    assert store.list_open_runs() == []


# --- travaux par lot ---------------------------------------------------------


def test_cycle_de_vie_d_un_travail_par_lot(db_neuve):
    doc_id = _document()
    run2, _ = store.start_run(doc_id, mode="economique")
    store.record_batch_job("job-abc", run_id=run2, document_id=doc_id,
                           endpoint="/v1/chat/completions", kind="extraction",
                           status="QUEUED", input_file_id="file-1",
                           fiche_seq_map={"seg-000": 0, "seg-001": 1})
    assert len(store.open_batch_jobs()) == 1
    store.refresh_batch_job("job-abc", status="RUNNING", total_requests=2)
    assert store.open_batch_jobs()[0]["status"] == "RUNNING"
    store.refresh_batch_job("job-abc", status="SUCCESS", output_file_id="file-2",
                            succeeded_requests=2, failed_requests=0)
    assert store.open_batch_jobs() == [], "un job terminal sort de la liste ouverte"
    travail = store.get_batch_job("job-abc")
    assert json.loads(travail["fiche_seq_map_json"])["seg-001"] == 1
    assert travail["is_terminal"] == 1


# --- export / import --------------------------------------------------------


def test_membres_de_l_archive(db_neuve):
    _, _, archive = _archive_complete()
    noms = zipfile.ZipFile(io.BytesIO(archive)).namelist()
    assert set(noms) == {"manifest.json", "documents.json", "runs.json",
                         "fiches.json", f"ocr/{SHA}.json.gz"}


def test_reimport_idempotent(db_neuve):
    _, _, archive = _archive_complete()
    rapport = store.import_bundle(archive)
    assert rapport["documents_existants"] == 1
    assert rapport["runs_ignores"] == 2
    assert rapport["ocr_ignores"] == 1
    assert rapport["runs_ajoutes"] == 0


def test_suppression_en_cascade(db_neuve):
    doc_id, _, _ = _archive_complete()
    store.delete_document(doc_id)
    assert store.list_documents() == [] and store.list_runs() == []
    with store._connect() as con:
        for table in ("fiches", "runs", "ocr_cache", "batch_jobs"):
            reste = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert reste == 0, f"cascade incomplète sur {table} ({reste} lignes)"


def test_restauration_complete_apres_suppression(db_neuve):
    doc_id, _, archive = _archive_complete()
    store.delete_document(doc_id)
    rapport = store.import_bundle(archive)
    assert rapport["documents_ajoutes"] == 1 and rapport["runs_ajoutes"] == 2
    assert rapport["fiches_ajoutees"] == 2 and rapport["ocr_ajoutes"] == 1
    assert store.has_ocr_payload(store.list_documents()[0]["id"])
    nouveau = next(r for r in store.list_runs() if r["nb_ok"])
    restaure = store.load_run_as_parsed_data(nouveau["id"])
    assert restaure["projects"][0]["_page_debut"] == 3
    assert restaure["projects"][0]["Presentation"]["Titre"] == "Restauration <tourbière>"
    assert store.load_failures(nouveau["id"])[0]["categorie"] == "quota"


# --- refus d'archives hostiles ----------------------------------------------


def _archive_hostile(mutation=None, version=1):
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"format": store.FORMAT_ARCHIVE, "version": version}))
        for membre in ("documents.json", "runs.json", "fiches.json"):
            zf.writestr(membre, "[]")
        if mutation:
            mutation(zf)
    return tampon.getvalue()


@pytest.mark.parametrize("octets, cas", [
    (_archive_hostile(lambda zf: zf.writestr("../evil.txt", "x")), "traversée de chemin"),
    (_archive_hostile(lambda zf: zf.writestr("rex.db", b"SQLite format 3\x00")), "fichier sqlite"),
    (_archive_hostile(lambda zf: zf.writestr("ocr/pasunhash.json.gz", b"x")), "nom de charge OCR"),
    (_archive_hostile(version=99), "version future"),
    (b"pas un zip", "zip illisible"),
    (b"x" * (store.MAX_ARCHIVE_BYTES + 1), "taille maximale"),
])
def test_archive_hostile_refusee(db_neuve, octets, cas):
    with pytest.raises(store.BundleInvalide):
        store.import_bundle(octets)


# --- verdict de conformité ---------------------------------------------------

RAPPORT = {"version": 1, "statut": "non_conforme", "corrections": [],
           "erreurs": [{"chemin": "Presentation/Région", "validateur": "enum",
                        "message": "valeur hors énumération contrôlée"}]}


def test_verdict_persiste_et_se_relit(db_neuve):
    doc_id = _document()
    run_id, _ = store.start_run(doc_id, mode="rapide")
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", data=FICHE,
                       validation_status="non_conforme", validation_errors=RAPPORT)
    ligne = store.list_fiches(run_id)[0]
    assert ligne["validation_status"] == "non_conforme"
    assert json.loads(ligne["validation_errors_json"])["erreurs"][0]["chemin"] \
        == "Presentation/Région"


def test_verdict_reinjecte_au_rechargement(db_neuve):
    doc_id = _document()
    run_id, _ = store.start_run(doc_id, mode="rapide")
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", data=FICHE,
                       validation_status="corrige", validation_errors=RAPPORT)
    store.finish_run(run_id, status="termine")
    projet = store.load_run_as_parsed_data(run_id)["projects"][0]
    assert projet["_validation_status"] == "corrige"
    assert projet["_validation_errors_json"]


def test_fiche_non_conforme_reste_ok(db_neuve):
    """
    `status` et `validation_status` sont orthogonaux. Tout autre statut ferait
    disparaître la fiche du tableau, de l'export et du rechargement — donc
    précisément les lignes qu'un expert doit corriger.
    """
    doc_id = _document()
    run_id, _ = store.start_run(doc_id, mode="rapide")
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", data=FICHE,
                       validation_status="non_conforme", validation_errors=RAPPORT)
    store.finish_run(run_id, status="termine")
    assert len(store.load_run_as_parsed_data(run_id)["projects"]) == 1


def test_reupsert_ecrase_le_verdict(db_neuve):
    """
    Le bug latent : les deux colonnes manquaient au DO UPDATE SET, donc une
    relance — ou une récolte de lot réécrivant une ligne `en_attente` — aurait
    gardé un verdict périmé en face de données neuves.
    """
    doc_id = _document()
    run_id, _ = store.start_run(doc_id, mode="rapide")
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", data=FICHE,
                       validation_status="non_conforme", validation_errors=RAPPORT)
    # Réécriture avec un verdict neuf
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", data=FICHE,
                       validation_status="conforme")
    ligne = store.list_fiches(run_id)[0]
    assert ligne["validation_status"] == "conforme"
    assert ligne["validation_errors_json"] is None
    # Réécriture SANS verdict : la colonne redevient NULL, elle ne conserve rien.
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", data=FICHE)
    assert store.list_fiches(run_id)[0]["validation_status"] is None


def test_statut_de_validation_inconnu_refuse(db_neuve):
    doc_id = _document()
    run_id, _ = store.start_run(doc_id, mode="rapide")
    with pytest.raises(ValueError, match="statut de validation"):
        store.upsert_fiche(run_id, doc_id, 0, status="ok", validation_status="parfait")


def test_verdict_survit_a_l_archive(db_neuve):
    """
    Le second bug latent : export_bundle et import_bundle énuméraient leurs
    colonnes et omettaient les deux, si bien qu'un aller-retour d'archive perdait
    le verdict en silence — tout en comptant la fiche comme importée.
    """
    doc_id = _document()
    run_id, _ = store.start_run(doc_id, mode="rapide")
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", page_debut=3,
                       page_fin=5, data=FICHE, validation_status="corrige",
                       validation_errors=RAPPORT)
    store.finish_run(run_id, status="termine")
    archive = store.export_bundle(include_ocr=True)

    store.delete_document(doc_id)
    rapport = store.import_bundle(archive)
    assert rapport["fiches_ajoutees"] == 1
    nouveau = store.list_runs()[0]
    ligne = store.list_fiches(nouveau["id"])[0]
    assert ligne["validation_status"] == "corrige"
    assert json.loads(ligne["validation_errors_json"]) == RAPPORT


def test_archive_au_verdict_hostile_refusee(db_neuve):
    """import_bundle insère en SQL direct : le garde-fou d'upsert_fiche ne joue pas."""
    doc_id = _document()
    run_id, _ = store.start_run(doc_id, mode="rapide")
    store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", data=FICHE)
    store.finish_run(run_id, status="termine")
    archive = store.export_bundle(include_ocr=False)

    tampon = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        with zipfile.ZipFile(tampon, "w") as cible:
            for nom in source.namelist():
                donnees = source.read(nom)
                if nom == "fiches.json":
                    fiches = json.loads(donnees)
                    fiches[0]["validation_status"] = "parfait"
                    donnees = json.dumps(fiches).encode()
                cible.writestr(nom, donnees)
    with pytest.raises(store.BundleInvalide, match="statut de validation"):
        store.import_bundle(tampon.getvalue())


def test_archive_refusee_ne_modifie_rien(db_neuve):
    _document()
    avant = store.historique_stats()
    with pytest.raises(store.BundleInvalide):
        store.import_bundle(_archive_hostile(version=99))
    assert store.historique_stats() == avant
