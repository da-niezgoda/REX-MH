"""Canari de concurrence pour pipeline.extraire_fiches — aucun appel API."""
import json

import httpx
import pytest

import pipeline as p
from faux import JETONS_CACHE, JETONS_PROMPT, FauxClient, Inverseur

FMT = p.json_schema_format("t", {"type": "object", "properties": {"a": {"type": "string"}},
                                 "required": ["a"]})
CLE = p.cle_cache_prompt("extraction", "prompt système", p.MODEL_EXTRACTION)
TRACES = {"ocr": "mistral-ocr-4-0", "segmentation": "mistral-small-2506"}


def travaux(n):
    return [{"index": i, "titre": f"Fiche {i}", "debut": i + 1, "fin": i + 1,
             "contenu": json.dumps({"pages": [{"page_number": i + 1, "content": "x"}]})}
            for i in range(n)]


def client(*, croiser=False, **kw):
    return FauxClient(cle_attendue=CLE,
                      inverseur=Inverseur() if croiser else None, **kw)


def lancer(c, n, **kw):
    """Renvoie (fiches, echecs, usage, ordre d'achèvement observé)."""
    vus = []
    fiches, echecs, usage = p.extraire_fiches(
        c, travaux(n), prompt_systeme="prompt système", response_format=FMT,
        prompt_cache_key=CLE, modeles_traces=TRACES,
        on_resultat=lambda i, f, e, u: vus.append(i), **kw)
    return fiches, echecs, usage, vus


@pytest.fixture(scope="module")
def nominal():
    """Un run de 7 fiches sans incident, réutilisé par plusieurs assertions."""
    c = client(croiser=True)
    fiches, echecs, usage, vus = lancer(c, 7)
    return c, fiches, echecs, usage, vus


def test_echauffement_puis_fan_out(nominal):
    """La fiche 0 part SEULE : c'est elle qui écrit le préfixe de cache."""
    c, *_ = nominal
    assert c.chat.pages[0] == 1, c.chat.pages
    assert c.chat.max_en_vol <= p.MAX_CONCURRENCE_EXTRACTION, c.chat.max_en_vol
    assert c.chat.max_en_vol > 1, "les fiches 1..N doivent s'éventailler"


def test_ordre_document_retabli(nominal):
    """L'ordre du document est rétabli malgré un achèvement désordonné."""
    _, fiches, _, _, vus = nominal
    assert [f["_segment_index"] for f in fiches] == list(range(7)), fiches
    assert vus != sorted(vus), \
        "l'achèvement doit bien être désordonné, sinon le tri n'est pas testé"


def test_metadonnees_propagees(nominal):
    _, fiches, _, _, _ = nominal
    assert [f["_project_title"] for f in fiches] == [f"Fiche {i}" for i in range(7)]
    assert fiches[3]["_page_debut"] == 4
    assert fiches[3]["_model_extraction"] == "mistral-medium-2508"
    assert fiches[0]["_model_ocr"] == "mistral-ocr-4-0"
    assert fiches[0]["_prompt_hash"] == CLE[-16:]


def test_cache_engage_sur_les_fiches_suivantes(nominal):
    _, _, _, usage, _ = nominal
    assert usage["appels"] == 7
    assert usage["prompt_tokens"] == JETONS_PROMPT * 7
    assert usage["cached_tokens"] == JETONS_CACHE * 6, usage


def test_429_isole_devient_un_echec_nomme():
    """Un 429 sur une fiche devient un échec nommé — les autres passent."""
    c = client(echouer={4})
    fiches, echecs, _, _ = lancer(c, 7)
    assert (len(fiches), len(echecs)) == (6, 1)
    assert echecs[0]["index"] == 3 and echecs[0]["categorie"] == "quota"
    assert echecs[0]["reessayable"] is True and echecs[0]["pages"] == (4, 4)
    assert echecs[0]["trace"] and "SDKError" in echecs[0]["error"]


def test_echec_de_l_echauffement_ne_bloque_pas_le_run():
    """Le cache sera manqué, ce qui est une régression de coût, pas de justesse."""
    c = client(echouer={1})
    fiches, echecs, _, _ = lancer(c, 5)
    assert len(fiches) == 4 and echecs[0]["index"] == 0


def test_bug_a_l_echauffement_avorte_le_run():
    """
    Un bug de NOTRE code se reproduirait à l'identique sur les N fiches
    suivantes : inutile de les payer.
    """
    c = client(lever={1})
    with pytest.raises(AttributeError, match="REXPrompt"):
        lancer(c, 20)
    assert len(c.chat.pages) == 1, f"aucun fan-out ne doit avoir eu lieu : {c.chat.pages}"


def test_bug_dans_le_pool_sort_nomme():
    """Le même bug sur une fiche du pool ne fait pas tomber le run."""
    c = client(lever={4})
    fiches, echecs, _, _ = lancer(c, 7)
    assert (len(fiches), len(echecs)) == (6, 1)
    assert echecs[0]["categorie"] == "bug", echecs[0]
    assert echecs[0]["reessayable"] is False
    assert "REXPrompt" in echecs[0]["error"]
    assert "AttributeError" in echecs[0]["trace"]


def test_peu_de_fiches_saute_l_echauffement():
    """
    Sous `SEUIL_ECHAUFFEMENT`, l'échauffement n'est pas rentable : il sérialise le
    premier appel pour n'économiser 90 % que sur UNE fiche. Mesuré sur l'extrait
    de 18 pages : 68 s avec, 32 s sans. Les 2 fiches doivent donc partir ensemble.
    """
    c = client(croiser=True)
    lancer(c, 2)
    assert c.chat.max_en_vol == 2, \
        f"les 2 fiches doivent partir ensemble, max en vol {c.chat.max_en_vol}"


def test_seuil_echauffement_atteint_reserialise():
    """Au seuil, l'échauffement reprend : la fiche 0 repart seule."""
    assert p.SEUIL_ECHAUFFEMENT == 3
    c = client()
    lancer(c, p.SEUIL_ECHAUFFEMENT)
    assert c.chat.pages[0] == 1, c.chat.pages


def test_deja_echauffe_saute_la_phase_sequentielle():
    """
    Sans échauffement, les 4 fiches partent ensemble et s'apparient donc deux à
    deux — `max_en_vol` atteint 2 par construction, non par chance de timing.
    Avec l'échauffement, la fiche 0 partirait seule.
    """
    c = client(croiser=True)
    lancer(c, 4, deja_echauffe=True)
    assert c.chat.max_en_vol > 1, c.chat.max_en_vol


def test_readtimeout_est_un_timeout_reessayable():
    """
    Le `httpx.ReadTimeout` qui a perdu une fiche sur le recueil de 129 pages doit
    être classé « timeout » réessayable. Le SDK le réessaie bien (c'est une
    httpx.TimeoutException, retry_connection_errors=True) — encore faut-il que le
    budget lui en laisse le temps : voir le test d'invariant ci-dessous.
    """
    assert p.classer_erreur(httpx.ReadTimeout("read timed out")) == \
        ("timeout", True, None)


@pytest.mark.parametrize(
    "timeout_ms, retry",
    [
        (p.TIMEOUT_EXTRACTION_MS, p.RETRY_EXTRACTION),
        (p.TIMEOUT_SEGMENTATION_MS, p.RETRY_SEGMENTATION),
    ],
)
def test_budget_de_reessai_depasse_le_timeout(timeout_ms, retry):
    """
    INVARIANT : `max_elapsed_time` DOIT dépasser le `timeout_ms` par appel.

    Sinon un unique délai d'attente épuise tout le budget avant le moindre
    réessai : `retry_with_backoff` abandonne dès que « écoulé > max_elapsed_time ».
    Avec 120 s == 120 s, le premier ReadTimeout n'était jamais réessayé — c'est ce
    qui a transformé un délai d'attente en fiche perdue sur le recueil de 129
    pages. On exige de la marge pour au moins une tentative de plus.
    """
    assert retry.retry_connection_errors, \
        "un délai d'attente/coupure réseau doit être réessayable"
    assert retry.backoff.max_elapsed_time > timeout_ms, (
        f"budget de réessai {retry.backoff.max_elapsed_time} ms <= timeout par "
        f"appel {timeout_ms} ms : un délai d'attente ne serait jamais réessayé"
    )
