"""Canari de concurrence hors ligne pour pipeline.extraire_fiches — aucun appel API."""
import json
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline as p  # noqa: E402
from mistralai.client.errors import SDKError  # noqa: E402

FMT = p.json_schema_format("t", {"type": "object", "properties": {"a": {"type": "string"}},
                                 "required": ["a"]})
CLE = p.cle_cache_prompt("extraction", "prompt système", p.MODEL_EXTRACTION)
TRACES = {"ocr": "mistral-ocr-4-0", "segmentation": "mistral-small-2506"}


class FauxReponse:
    model = "mistral-medium-2508"
    def __init__(self, contenu, caches):
        msg = type("M", (), {"content": contenu})()
        self.choices = [type("C", (), {"message": msg})()]
        usage = type("U", (), {"prompt_tokens": 12000, "completion_tokens": 900,
                               "total_tokens": 12900,
                               "prompt_tokens_details": {"cached_tokens": caches}})()
        self.usage = usage


class FauxChat:
    """Journalise l'ordre d'envoi et la concurrence réelle, comme le ferait l'API."""
    def __init__(self, echouer=(), lever=()):
        self.echouer, self.lever = set(echouer), set(lever)
        self.appels, self.en_vol, self.max_en_vol = [], 0, 0
        self.verrou = threading.Lock()
        self.cache_chaud = False

    def complete(self, **kw):
        page = json.loads(kw["messages"][1]["content"])["pages"][0]["page_number"]
        assert kw["prompt_cache_key"] == CLE, "prompt_cache_key doit être transmis"
        assert kw["temperature"] == 0.0 and kw["random_seed"] == p.RANDOM_SEED
        with self.verrou:
            self.appels.append(page)
            self.en_vol += 1
            self.max_en_vol = max(self.max_en_vol, self.en_vol)
            chaud = self.cache_chaud
        try:
            time.sleep(0.05 if page % 2 else 0.01)   # achèvement désordonné
            if page in self.lever:
                # AttributeError = la classe d'erreur qu'un thread sans
                # ScriptRunContext obtient en lisant st.session_state.
                raise AttributeError('st.session_state has no attribute "REXPrompt"')
            if page in self.echouer:
                raise SDKError("boum", httpx.Response(
                    429, request=httpx.Request("POST", "http://x")))
            return FauxReponse(json.dumps({"a": f"fiche p{page}"}), 8832 if chaud else 0)
        finally:
            with self.verrou:
                self.en_vol -= 1
                self.cache_chaud = True


class FauxClient:
    def __init__(self, **kw):
        self.chat = FauxChat(**kw)


def travaux(n):
    return [{"index": i, "titre": f"Fiche {i}", "debut": i + 1, "fin": i + 1,
             "contenu": json.dumps({"pages": [{"page_number": i + 1, "content": "x"}]})}
            for i in range(n)]


def lancer(client, n, **kw):
    vus = []
    fiches, echecs, usage = p.extraire_fiches(
        client, travaux(n), prompt_systeme="prompt système", response_format=FMT,
        prompt_cache_key=CLE, modeles_traces=TRACES,
        on_resultat=lambda i, f, e, u: vus.append(i), **kw)
    return fiches, echecs, usage, vus


# 1. Échauffement : la fiche 0 part SEULE, avant toute autre.
c = FauxClient()
fiches, echecs, usage, vus = lancer(c, 7)
assert c.chat.appels[0] == 1, c.chat.appels
assert c.chat.max_en_vol <= p.MAX_CONCURRENCE_EXTRACTION, c.chat.max_en_vol
assert c.chat.max_en_vol > 1, "les fiches 1..N doivent s'éventailler"
print(f"échauffement puis fan-out : envois {c.chat.appels}, max en vol {c.chat.max_en_vol}")

# 2. Ordre document restauré malgré un achèvement désordonné.
assert [f["_segment_index"] for f in fiches] == list(range(7)), fiches
assert vus != sorted(vus), "l'achèvement doit bien être désordonné"
assert [f["_project_title"] for f in fiches] == [f"Fiche {i}" for i in range(7)]
assert fiches[3]["_page_debut"] == 4 and fiches[3]["_model_extraction"] == "mistral-medium-2508"
assert fiches[0]["_model_ocr"] == "mistral-ocr-4-0" and fiches[0]["_prompt_hash"] == CLE[-16:]
print(f"ordre rétabli : achèvement {vus} -> {[f['_segment_index'] for f in fiches]}")

# 3. Le cache s'engage sur les fiches 2+.
assert usage["appels"] == 7 and usage["prompt_tokens"] == 84000
assert usage["cached_tokens"] == 8832 * 6, usage
print(f"usage cumulé : {usage}, taux {p.taux_cache(usage):.0%}")

# 4. Un 429 sur une fiche devient un échec nommé — les autres passent.
c = FauxClient(echouer={4})
fiches, echecs, usage, _ = lancer(c, 7)
assert len(fiches) == 6 and len(echecs) == 1, (len(fiches), len(echecs))
assert echecs[0]["index"] == 3 and echecs[0]["categorie"] == "quota"
assert echecs[0]["reessayable"] is True and echecs[0]["pages"] == (4, 4)
assert echecs[0]["trace"] and "SDKError" in echecs[0]["error"]
print(f"échec isolé : {echecs[0]['titre']} p.{echecs[0]['pages']} -> {echecs[0]['categorie']}")

# 5. Échec de l'échauffement : on continue quand même.
c = FauxClient(echouer={1})
fiches, echecs, usage, _ = lancer(c, 5)
assert len(fiches) == 4 and echecs[0]["index"] == 0, (len(fiches), echecs)
print(f"échec de l'échauffement : le run continue, {len(fiches)} fiches extraites")

# 6a. Un bug de NOTRE code sur l'échauffement avorte le run : il se reproduirait
#     à l'identique sur les N fiches suivantes, inutile de les payer.
c = FauxClient(lever={1})
try:
    lancer(c, 20)
    raise AssertionError("un bug sur l'échauffement doit avorter le run")
except AttributeError as err:
    assert len(c.chat.appels) == 1, f"aucun fan-out ne doit avoir eu lieu : {c.chat.appels}"
    print(f"bug sur l'échauffement : run avorté après 1 appel — {err}")

# 6b. Le même bug sur une fiche du pool ne fait pas tomber le run, mais sort
#     nommé, avec sa trace et une catégorie qui le distingue d'un souci d'API.
c = FauxClient(lever={4})
fiches, echecs, usage, _ = lancer(c, 7)
assert len(fiches) == 6 and len(echecs) == 1, (len(fiches), len(echecs))
assert echecs[0]["categorie"] == "bug" and echecs[0]["reessayable"] is False, echecs[0]
assert "REXPrompt" in echecs[0]["error"] and "AttributeError" in echecs[0]["trace"]
print(f"bug sur une fiche du pool : catégorie « {echecs[0]['categorie']} », "
      f"{len(fiches)} fiches conservées")

# 7. deja_echauffe saute la phase séquentielle.
c = FauxClient()
lancer(c, 4, deja_echauffe=True)
assert c.chat.max_en_vol > 1
print(f"deja_echauffe=True : pas de phase séquentielle, max en vol {c.chat.max_en_vol}")

# 8. Aucun worker ne touche st.* : streamlit doit rester absent du processus.
assert "streamlit" not in sys.modules, "pipeline.py a importé streamlit"
print("streamlit toujours absent après exécution concurrente : OK")

print("\ncanari de concurrence : toutes les vérifications passent.")
