"""
Intégration hors ligne : app.py + pipeline.py + store.py avec un faux client.

Aucun appel API. Vérifie le chemin complet — OCR, cache OCR, segmentation,
extraction parallèle, persistance, rechargement, export Excel, mode par lot.
"""
import json
import os
import re
import sys
from pathlib import Path
import tempfile

CHEMIN = os.path.join(tempfile.mkdtemp(), "rex.db")
os.environ["REX_DB_PATH"] = CHEMIN
os.environ["MISTRAL_API_KEY"] = "factice-pour-le-test"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402
import app  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
import httpx  # noqa: E402
from mistralai.client.errors import SDKError  # noqa: E402
from mistralai.client.models import OCRResponse  # noqa: E402

NB_PAGES = 18
NB_FICHES = 7

# --- fausse réponse OCR, construite avec les vrais modèles du SDK -------------
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
OCR = OCRResponse.model_validate(brut)
assert OCR.pages[0].blocks[0].content == "Fiche 1", "les blocs doivent survivre"
assert OCR.pages[0].confidence_scores.average_page_confidence_score == 0.97

SEGMENTS = {"Liste": [
    {"Titre": f"Projet {i}", "PageDebut": 1 + i * 2, "PageFin": 2 + i * 2}
    for i in range(NB_FICHES - 2)
] + [
    {"Titre": "Segment à page zéro", "PageDebut": 0, "PageFin": 3},   # le bug historique
    {"Titre": "Segment hors document", "PageDebut": 90, "PageFin": 95},
]}

def _instance_conforme(noeud):
    """
    Plus petite instance valide d'un (sous-)schéma REX.

    Bâtie depuis le schéma lui-même plutôt qu'écrite à la main : le schéma
    compte 10 sections et des dizaines d'énumérations métier, et une fixture
    figée se désynchroniserait au premier ajout de champ (tâche 5).
    """
    if noeud.get("type") == "object":
        return {cle: _instance_conforme(noeud["properties"][cle])
                for cle in noeud.get("required", [])}
    if noeud.get("type") == "array":
        return [_instance_conforme(noeud["items"])]
    if noeud.get("enum"):
        return noeud["enum"][0]
    if noeud.get("type") == "string":
        motif = noeud.get("pattern")
        if not motif:
            return ""
        # Premier candidat qui satisfait le `pattern` du schéma (année, date,
        # URL, plage de pages…). Évite d'inscrire les motifs en dur ici.
        for candidat in ("", "2020", "01/03/2020", "https://example.org/rex", "3-5"):
            if re.fullmatch(motif, candidat):
                return candidat
        raise AssertionError(f"aucun candidat ne satisfait le motif {motif!r}")
    if noeud.get("type") in ("integer", "number"):
        return noeud.get("minimum", 0)
    return ""


def _fiche_de_test(schema):
    """Fiche conforme, puis surchargée là où le test doit vérifier un cas précis."""
    fiche = _instance_conforme(schema)
    # Caractères à échapper dans le rendu HTML.
    fiche["Presentation"]["Titre"] = "Restauration <tourbière> & marais"
    fiche["Presentation"]["Nom de l'organisme"] = "OiEau"
    # Format du schéma ACTUEL (JJ/MM/AAAA) : la tâche 5 le réduira à AAAA.
    fiche["Enjeux"]["date_debut"] = "01/03/2019"
    fiche["Enjeux"]["date_fin"] = "30/09/2021"
    fiche["Enjeux"]["enjeux"] = [
        fiche["Enjeux"]["enjeux"][0],
        schema["properties"]["Enjeux"]["properties"]["enjeux"]["items"]["enum"][1],
    ]
    # Liste HORS section Enjeux : le cas qui ressortait en « ['Sentier'] » dans
    # l'Excel, crochets compris.
    valorisation = schema["properties"]["Valorisation"]["properties"]
    fiche["Valorisation"]["type_valorisation"] = [
        valorisation["type_valorisation"]["items"]["enum"][0],
        valorisation["type_valorisation"]["items"]["enum"][1],
    ]
    fiche["Valorisation"]["url"] = "https://example.org/rex"
    return fiche


class FauxFichiers:
    def __init__(self): self.televerses, self.supprimes = [], []
    def upload(self, **kw):
        self.televerses.append(kw)
        return type("F", (), {"id": f"file-{len(self.televerses)}"})()
    def get_signed_url(self, **kw): return type("S", (), {"url": "https://signed"})()
    def delete(self, **kw): self.supprimes.append(kw["file_id"])
    def download(self, **kw): return type("R", (), {"text": self.contenu})()


class FauxChat:
    def __init__(self, echouer=()):
        self.echouer, self.appels, self.cles = set(echouer), [], []
    def complete(self, **kw):
        self.appels.append(kw["model"])
        self.cles.append(kw.get("prompt_cache_key"))
        contenu = kw["messages"][1]["content"]
        if kw["model"] == pipeline.MODEL_SEGMENTATION:
            payload = json.dumps(SEGMENTS)
        else:
            page = json.loads(contenu)["pages"][0]["page_number"]
            if page in self.echouer:
                # Vraie exception du SDK, pour que classer_erreur soit exercée
                # comme en production (429 => « quota », réessayable).
                raise SDKError("saturé", httpx.Response(
                    429, request=httpx.Request("POST", "https://api.mistral.ai")))
            payload = json.dumps(FICHE)
        caches = 8832 if len([c for c in self.cles if c == kw.get("prompt_cache_key")]) > 1 else 0
        msg = type("M", (), {"content": payload})()
        usage = type("U", (), {"prompt_tokens": 12000, "completion_tokens": 900,
                               "total_tokens": 12900,
                               "prompt_tokens_details": {"cached_tokens": caches}})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()],
                              "usage": usage, "model": "mistral-medium-2508"})()


class FauxOcr:
    def __init__(self): self.appels = 0
    def process(self, **kw):
        self.appels += 1
        assert kw["include_blocks"] is True, "les paramètres OCR doivent passer"
        return OCR


class FauxJobs:
    def __init__(self): self.crees, self.sorties = [], []
    def create(self, **kw):
        self.crees.append(kw)
        return type("J", (), {"id": "batch-1", "status": "QUEUED"})()
    def get(self, **kw):
        return type("J", (), {"id": kw["job_id"], "status": "SUCCESS",
                              "outputs": self.sorties, "output_file": None,
                              "error_file": None, "errors": [],
                              "total_requests": len(self.sorties),
                              "succeeded_requests": len(self.sorties),
                              "failed_requests": 0})()


class FauxClient:
    def __init__(self, echouer=()):
        self.files, self.chat = FauxFichiers(), FauxChat(echouer)
        self.ocr = FauxOcr()
        self.batch = type("B", (), {"jobs": FauxJobs()})()


CLIENT = FauxClient()
app.client_mistral = lambda: CLIENT

store.init_db(CHEMIN)
st.session_state.REXSchema = app.load_schema("REX.schema.json")
st.session_state.REXListSchema = app.load_schema("REXlist.schema.json")
st.session_state.REXPrompt = app.load_prompt("REXPrompt.md", st.session_state.REXSchema)
st.session_state.listPrompt = app.load_prompt("listPrompt.md", st.session_state.REXListSchema)
assert all(st.session_state[k] for k in app.CLES_PROMPTS), "prompts/schémas illisibles"

FICHE = _fiche_de_test(st.session_state.REXSchema)
ATTENDU_VALORISATION = ", ".join(FICHE["Valorisation"]["type_valorisation"])
ATTENDU_ENJEUX = ", ".join(FICHE["Enjeux"]["enjeux"])

PDF = b"%PDF-1.4 faux contenu de test"
etapes = []


def suivi(avancement, message, **details):
    etapes.append((round(avancement, 3), message, details.get("phase")))


# === 1. Mode rapide, premier passage =========================================
res = app.parse_pdf_document(PDF, "recueil-test.pdf", progress_callback=suivi)
assert res["statut"] == "partiel", res["statut"]
assert len(res["projects"]) == NB_FICHES - 2, len(res["projects"])
assert len(res["failures"]) == 2, res["failures"]
assert [e["categorie"] for e in res["failures"]] == ["segment_invalide"] * 2
assert "page zéro" in res["failures"][0]["titre"].lower()
assert CLIENT.ocr.appels == 1
assert res["ocr_cache_hit"] is False
print(f"1. rapide : {len(res['projects'])} fiches, {len(res['failures'])} échecs nommés")

# Ordre document, malgré un achèvement concurrent désordonné
assert [f["_segment_index"] for f in res["projects"]] == list(range(NB_FICHES - 2))
# La clé de cache de prompt est bien transmise à chaque appel d'extraction
cles_extraction = [c for m, c in zip(CLIENT.chat.appels, CLIENT.chat.cles)
                   if m == pipeline.MODEL_EXTRACTION]
assert len(set(cles_extraction)) == 1 and cles_extraction[0].startswith("rex-extraction-")
print(f"   clé de cache unique pour les {len(cles_extraction)} extractions : "
      f"{cles_extraction[0]}")
assert res["usage"]["extraction"]["cached_tokens"] > 0
print(f"   taux de cache mesuré : {pipeline.taux_cache(res['usage']['extraction']):.0%}")

# Le PDF téléversé est supprimé (il fuyait à chaque run)
assert CLIENT.files.supprimes == ["file-1"], CLIENT.files.supprimes
print(f"   fichier téléversé supprimé : {CLIENT.files.supprimes}")

# Progression : monotone, dans [0,1], et les phases se suivent
avancements = [e[0] for e in etapes]
assert avancements == sorted(avancements), avancements
assert 0.0 <= min(avancements) and max(avancements) == 1.0
phases = [e[2] for e in etapes if e[2]]
assert phases[0] == "upload" and phases[-1] == "extraction"
print(f"   progression monotone {avancements[0]} → {avancements[-1]}, "
      f"phases {sorted(set(phases))}")

# === 2. Persistance ==========================================================
run = store.get_run(res["run_id"])
assert run["status"] == "partiel" and run["mode"] == "rapide"
assert run["model_ocr"] == "mistral-ocr-4-0" and run["model_extraction"] == "mistral-medium-2508"
assert run["model_segmentation"] == "mistral-medium-2508"  # le faux client renvoie ce modèle
assert run["prompt_extraction_sha256"] and run["schema_rex_sha256"]
assert run["prompt_tokens"] == 12000 * (NB_FICHES - 1)   # 5 extractions + 1 segmentation
assert len(store.list_fiches(res["run_id"])) == NB_FICHES
assert len(store.load_failures(res["run_id"])) == 2
print(f"2. persistance : run {run['status']}, {run['prompt_tokens']} jetons, "
      f"{len(store.list_fiches(res['run_id']))} lignes de fiche")

# data_json ne doit pas transporter les clés « _ » (additionalProperties: false)
stocke = json.loads(store.list_fiches(res["run_id"], status="ok")[0]["data_json"])
assert not any(k.startswith("_") for k in stocke)
import jsonschema  # noqa: E402
jsonschema.Draft7Validator(st.session_state.REXSchema).validate(stocke)
print("   fiche stockée conforme à REX.schema.json (clés « _ » exclues)")

# === 3. Deuxième passage : l'OCR vient du cache ==============================
res2 = app.parse_pdf_document(PDF, "recueil-test.pdf")
assert res2["ocr_cache_hit"] is True, "le cache OCR n'a pas été utilisé"
assert CLIENT.ocr.appels == 1, "l'OCR a été rappelé alors qu'il était en cache"
assert len(CLIENT.files.televerses) == 1, "le PDF a été retéléversé pour rien"
assert res2["run_id"] != res["run_id"], "un second traitement doit être un run distinct"
print(f"3. cache OCR : 2e passage sans appel OCR ({CLIENT.ocr.appels} au total), "
      f"run {res2['run_id']}")

# === 4. Rechargement et export ==============================================
data = store.load_run_as_parsed_data(res["run_id"])
assert data["filename"] == "recueil-test.pdf" and len(data["projects"]) == NB_FICHES - 2
assert data["projects"][0]["_model_extraction"] == "mistral-medium-2508"

plat = app.flatten_project_data(data["projects"][0])
# Le défaut client : une liste hors section Enjeux sortait avec ses crochets.
assert plat["type_valorisation"] == ATTENDU_VALORISATION, plat["type_valorisation"]
assert "," in ATTENDU_VALORISATION, "il faut bien deux valeurs pour tester la jointure"
assert plat["enjeux"] == ATTENDU_ENJEUX, plat["enjeux"]
assert plat["_model_extraction"] == "mistral-medium-2508", "traçabilité dans l'export"
assert plat["_prompt_hash"], "empreinte de prompt dans l'export"
print(f"4. export : type_valorisation = {plat['type_valorisation']!r} (sans crochets), "
      f"{len(plat)} colonnes")

octets = app.create_excel_download(data["projects"])
assert octets[:2] == b"PK" and len(octets) > 4000
# Relu depuis l'archive xlsx elle-même : openpyxl n'est pas une dépendance du
# projet (xlsxwriter écrit, personne ne relit en production).
import io  # noqa: E402
import zipfile  # noqa: E402
with zipfile.ZipFile(io.BytesIO(octets)) as zf:
    chaines = zf.read("xl/sharedStrings.xml").decode("utf-8")
assert ATTENDU_VALORISATION in chaines, "valeurs jointes absentes du classeur"
assert "_model_extraction" in chaines, "traçabilité absente du classeur"
assert "[" not in ATTENDU_VALORISATION and f"['{FICHE['Valorisation']['type_valorisation'][0]}'" \
    not in chaines, "une liste Python brute a atteint le classeur"
print(f"   classeur : {len(octets) / 1024:.0f} Ko, valeurs jointes et "
      f"traçabilité présentes")
assert app._nom_export("IFD_FICJOINT.PDF") == "IFD_FICJOINT_REX_export.xlsx"
assert app._nom_export("recueil.pdf") == "recueil_REX_export.xlsx"
print(f"   nom d'export insensible à la casse : {app._nom_export('IFD_FICJOINT.PDF')}")

# === 5. Échappement HTML ====================================================
rendu = app.format_expanded_data(data["projects"][0])
assert "<tourbière>" not in rendu and "&lt;tourbière&gt;" in rendu, "échappement manquant"
assert "&amp; marais" in rendu
malveillant = {"Presentation": {"Titre": "<script>alert(1)</script>"},
               "Valorisation": {"url": "javascript:alert(1)",
                                "type_valorisation": ["a", "b"]}}
rendu2 = app.format_expanded_data(malveillant)
assert "<script>" not in rendu2 and "&lt;script&gt;" in rendu2
assert 'href="javascript:' not in rendu2, "schéma d'URL exécutable non filtré"
assert "a, b" in rendu2, "une liste hors Enjeux doit être jointe"
print("5. échappement HTML : balises neutralisées, javascript: filtré, listes jointes")

# === 6. Relance des fiches en échec ========================================
CLIENT3 = FauxClient(echouer={5})
app.client_mistral = lambda: CLIENT3
res3 = app.parse_pdf_document(PDF, "recueil-test.pdf")
echecs_api = [e for e in res3["failures"] if e["categorie"] != "segment_invalide"]
assert len(echecs_api) == 1 and echecs_api[0]["reessayable"] is True
print(f"6. échec d'API isolé : {echecs_api[0]['titre']} → {echecs_api[0]['categorie']}, "
      f"{len(res3['projects'])} fiches conservées")

app.client_mistral = lambda: FauxClient()          # l'API redevient saine
bilan = app.relancer_fiches(res3["document_id"], res3["run_id"],
                            [e["index"] for e in echecs_api])
assert bilan and bilan["relancees"] == 1 and bilan["encore_en_echec"] == 0, bilan
apres = store.load_run_as_parsed_data(res3["run_id"])
assert len(apres["projects"]) == NB_FICHES - 2, len(apres["projects"])
print(f"   relance : {bilan['relancees']} fiche récupérée, "
      f"{len(apres['projects'])} fiches au total, sans nouvel OCR")

# === 7. Mode économique : soumission puis récolte ===========================
LOT = FauxClient()
app.client_mistral = lambda: LOT
res4 = app.parse_pdf_document(PDF, "recueil-test.pdf", mode="economique")
assert res4["statut"] == "en_attente" and res4["job_id"] == "batch-1"
assert LOT.files.televerses[0]["purpose"] == "batch", "purpose doit être 'batch'"
jsonl = LOT.files.televerses[0]["file"]["content"].decode("utf-8").splitlines()
assert len(jsonl) == NB_FICHES - 2
premiere = json.loads(jsonl[0])
assert premiere["custom_id"] == "seg-000"
assert "model" not in premiere["body"], "le modèle est fixé au niveau du travail"
assert premiere["body"]["response_format"]["json_schema"]["strict"] is True
assert premiere["body"]["prompt_cache_key"].startswith("rex-extraction-")
assert LOT.batch.jobs.crees[0]["endpoint"] == "/v1/chat/completions"
assert LOT.batch.jobs.crees[0]["metadata"]["application"] == "rex-mh"
ouverts = store.open_batch_jobs()
assert len(ouverts) == 1 and ouverts[0]["job_id"] == "batch-1"
assert json.loads(ouverts[0]["fiche_seq_map_json"])["seg-000"] == 0
print(f"7. lot soumis : {len(jsonl)} lignes JSONL, purpose=batch, "
      f"custom_id {premiere['custom_id']}…, travail {res4['job_id']}")

# Récolte : une ligne OK, une en erreur, une carrément absente
LOT.batch.jobs.sorties = [
    {"custom_id": "seg-000", "response": {"status_code": 200, "body": {
        "choices": [{"message": {"content": json.dumps(FICHE)}}]}}},
    {"custom_id": "seg-001", "response": {"status_code": 500, "body": {}},
     "error": "serveur indisponible"},
    {"custom_id": "seg-002", "response": {"status_code": 200, "body": {
        "choices": [{"message": {"content": json.dumps(FICHE)}}]}}},
]  # seg-003 et seg-004 manquent volontairement
bilan = app.actualiser_travail_par_lot("batch-1")
assert bilan["recolte"] is True
assert (bilan["ok"], bilan["echecs"], bilan["manquants"]) == (2, 1, 2), bilan
assert store.open_batch_jobs() == [], "un travail terminé sort de la liste ouverte"
categories = {f["categorie"] for f in store.list_fiches(res4["run_id"], status="echec")}
# segment_invalide vient des deux segments refusés avant soumission ; « lot » de
# la ligne en erreur ; « absent_lot » des deux segments sans ligne de résultat.
assert categories == {"lot", "absent_lot", "segment_invalide"}, categories
# Le PDF (file-1) est supprimé juste après l'OCR, le JSONL du lot ensuite.
assert LOT.files.supprimes, "aucun fichier distant supprimé"
jsonl_id = LOT.files.televerses[-1] and [
    f"file-{i + 1}" for i, kw in enumerate(LOT.files.televerses)
    if kw["purpose"] == "batch"][0]
assert jsonl_id in LOT.files.supprimes, (
    f"le JSONL {jsonl_id} doit être supprimé après récolte : {LOT.files.supprimes}")
print(f"   récolte : {bilan['ok']} OK, {bilan['echecs']} en échec, "
      f"{bilan['manquants']} sans résultat (catégories {sorted(categories)})")

# === 8. Archive de l'historique ============================================
archive = store.export_bundle(include_ocr=True)
rapport = store.import_bundle(archive)
assert rapport["runs_ignores"] >= 4 and rapport["documents_existants"] == 1
stats = store.historique_stats()
print(f"8. archive : {len(archive) / 1024:.0f} Ko pour {stats['documents']} document, "
      f"{stats['runs']} runs, {stats['fiches']} fiches")

print("\nintégration hors ligne : toutes les vérifications passent.")
