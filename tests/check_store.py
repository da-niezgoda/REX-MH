"""Vérification hors ligne de store.py — aucun appel API, aucun streamlit."""
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHEMIN = os.path.join(tempfile.mkdtemp(), "rex.db")
os.environ["REX_DB_PATH"] = CHEMIN

import store  # noqa: E402

assert "streamlit" not in sys.modules, "store.py ne doit pas importer streamlit"
print("streamlit non importé : OK")

print("init_db ->", store.init_db(CHEMIN))
print("schema_version ->", store.schema_version())
print("list_documents ->", store.list_documents())

# --- documents + cache OCR ---------------------------------------------------
sha = "a" * 64
doc_id = store.get_or_create_document(sha, "recueil.pdf", size_bytes=1234)
assert store.get_or_create_document(sha, "autre-nom.pdf") == doc_id, "unicité sha256"
payload = json.dumps({"pages": [{"index": 0, "markdown": "# Fiche accentuée é"}]})
store.save_ocr_payload(
    doc_id, payload, cle_ocr="k1", model="mistral-ocr-4-0", pages_processed=18,
    avg_confidence=0.97, sdk_version="2.8.0",
)
assert store.get_ocr_payload(doc_id) == payload, "round-trip gzip"
assert store.get_ocr_payload(doc_id, cle_ocr="k1") == payload
assert store.get_ocr_payload(doc_id, cle_ocr="autre") is None, "clé OCR différente => pas de cache"
store.set_document_pages(doc_id, 18)
meta = store.get_ocr_meta(doc_id)
assert "payload_gz" not in meta and meta["payload_bytes"] == len(payload.encode())
assert store.has_ocr_payload(doc_id)
store.mark_ocr_payload_invalid(doc_id, "test")
assert store.get_ocr_payload(doc_id) is None and not store.has_ocr_payload(doc_id)
store.save_ocr_payload(doc_id, payload, cle_ocr="k1")  # réécriture => redevient valide
assert store.has_ocr_payload(doc_id)
print("documents + cache OCR : OK")

# --- runs + fiches -----------------------------------------------------------
run_id, uid = store.start_run(
    doc_id, mode="rapide", prompt_extraction_sha256="p1", schema_rex_sha256="s1"
)
try:
    store.start_run(doc_id, mode="rapide")
    raise AssertionError("un deuxième run en_cours doit être refusé")
except sqlite3.IntegrityError:
    print("index partiel un-seul-run-en-cours : OK")

store.set_run_segmentation(run_id, json.dumps({"Liste": [{"Titre": "A"}]}),
                           model_segmentation="mistral-small-2506")
store.set_run_models(run_id, model_ocr="mistral-ocr-4-0", model_extraction="mistral-medium-2508")

fiche = {"Presentation": {"Titre": "Restauration <tourbière>"}, "_project_title": "A",
         "_page_debut": 3, "_model_extraction": "x"}
store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A", page_debut=3, page_fin=5,
                   data=fiche, model_extraction="mistral-medium-2508", prompt_hash="p1",
                   usage={"prompt_tokens": 12000, "cached_tokens": 8800,
                          "completion_tokens": 900})
store.upsert_fiche(run_id, doc_id, 1, status="echec", titre="B", page_debut=6, page_fin=7,
                   error="SDKError: 429", categorie="quota")
# idempotence : réécrire seq=0 ne duplique pas
store.upsert_fiche(run_id, doc_id, 0, status="ok", titre="A bis", page_debut=3, page_fin=5,
                   data=fiche, model_extraction="mistral-medium-2508", prompt_hash="p1")
assert len(store.list_fiches(run_id)) == 2, "UNIQUE(run_id, seq) => upsert"

stocke = json.loads(store.list_fiches(run_id, status="ok")[0]["data_json"])
assert not any(k.startswith("_") for k in stocke), "les clés _ ne vont pas dans data_json"

store.add_run_usage(run_id, prompt_tokens=12000, cached_tokens=8800, completion_tokens=900)
store.add_run_usage(run_id, prompt_tokens=12000, cached_tokens=11500, completion_tokens=800)
store.finish_run(run_id, status="partiel")
r = store.get_run(run_id)
assert (r["prompt_tokens"], r["cached_tokens"]) == (24000, 20300), "cumul incrémental"
assert r["status"] == "partiel" and r["finished_at"]

recharge = store.load_run_as_parsed_data(run_id)
assert recharge["filename"] == "recueil.pdf" and len(recharge["projects"]) == 1
p = recharge["projects"][0]
assert p["_project_title"] == "A bis" and p["_page_debut"] == 3
assert p["_model_ocr"] == "mistral-ocr-4-0" and p["_model_extraction"] == "mistral-medium-2508"
assert p["Presentation"]["Titre"] == "Restauration <tourbière>"
echecs = store.load_failures(run_id)
assert echecs == [{"index": 1, "titre": "B", "pages": (6, 7), "categorie": "quota",
                   "error": "SDKError: 429"}], echecs
print("runs + fiches + rechargement : OK")

runs = store.list_runs(doc_id)
assert len(runs) == 1 and runs[0]["nb_ok"] == 1 and runs[0]["nb_echec"] == 1
assert store.list_documents()[0]["nb_runs"] == 1
assert store.list_open_runs() == []

# --- travaux par lot ---------------------------------------------------------
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
j = store.get_batch_job("job-abc")
assert json.loads(j["fiche_seq_map_json"])["seg-001"] == 1 and j["is_terminal"] == 1
store.finish_run(run2, status="termine")
print("travaux par lot : OK")

print("stats ->", store.historique_stats())

# --- export / import ---------------------------------------------------------
archive = store.export_bundle(include_ocr=True, mistralai_version="2.8.0")
noms = zipfile.ZipFile(io.BytesIO(archive)).namelist()
assert set(noms) == {"manifest.json", "documents.json", "runs.json", "fiches.json",
                     f"ocr/{sha}.json.gz"}, noms
print(f"archive : {len(archive)} octets, membres {sorted(noms)}")

rapport = store.import_bundle(archive)
assert rapport["documents_existants"] == 1 and rapport["runs_ignores"] == 2, rapport
assert rapport["ocr_ignores"] == 1 and rapport["runs_ajoutes"] == 0, rapport
print("réimport idempotent :", rapport)

store.delete_document(doc_id)
assert store.list_documents() == [] and store.list_runs() == []
with store._connect() as con:
    for table in ("fiches", "runs", "ocr_cache", "batch_jobs"):
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert n == 0, f"cascade incomplète sur {table} ({n} lignes)"
print("suppression en cascade : OK")

rapport = store.import_bundle(archive)
assert rapport["documents_ajoutes"] == 1 and rapport["runs_ajoutes"] == 2, rapport
assert rapport["fiches_ajoutees"] == 2 and rapport["ocr_ajoutes"] == 1, rapport
assert store.has_ocr_payload(store.list_documents()[0]["id"])
nouveau = next(r for r in store.list_runs() if r["nb_ok"])
restaure = store.load_run_as_parsed_data(nouveau["id"])
assert restaure["projects"][0]["_page_debut"] == 3, restaure
assert restaure["projects"][0]["Presentation"]["Titre"] == "Restauration <tourbière>"
assert store.load_failures(nouveau["id"])[0]["categorie"] == "quota"
print("restauration complète :", rapport)

# --- refus d'archives hostiles ----------------------------------------------
def hostile(mutation, attendu):
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"format": store.FORMAT_ARCHIVE, "version": 1}))
        zf.writestr("documents.json", "[]")
        zf.writestr("runs.json", "[]")
        zf.writestr("fiches.json", "[]")
        mutation(zf)
    try:
        store.import_bundle(tampon.getvalue())
    except store.BundleInvalide as err:
        print(f"refusé ({attendu}) : {err}")
        return
    raise AssertionError(f"aurait dû être refusé : {attendu}")

hostile(lambda zf: zf.writestr("../evil.txt", "x"), "traversée de chemin")
hostile(lambda zf: zf.writestr("rex.db", b"SQLite format 3\x00"), "fichier sqlite")
hostile(lambda zf: zf.writestr("ocr/pasunhash.json.gz", b"x"), "nom de charge OCR")

def version_99():
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"format": store.FORMAT_ARCHIVE, "version": 99}))
        for m in ("documents.json", "runs.json", "fiches.json"):
            zf.writestr(m, "[]")
    return tampon.getvalue()

for octets, attendu in [
    (version_99(), "version future"),
    (b"pas un zip", "zip illisible"),
    (b"x" * (store.MAX_ARCHIVE_BYTES + 1), "taille maximale"),
]:
    try:
        store.import_bundle(octets)
        raise AssertionError(f"aurait dû être refusé : {attendu}")
    except store.BundleInvalide as err:
        print(f"refusé ({attendu}) : {err}")

avant = store.historique_stats()
try:
    store.import_bundle(version_99())
except store.BundleInvalide:
    pass
assert store.historique_stats() == avant, "une archive refusée ne doit rien modifier"
print("base intacte après refus : OK")

print("\nstore.py : toutes les vérifications passent.")
