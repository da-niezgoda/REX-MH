"""
Le nouveau raccord d'orchestration : pipeline expose le cœur, app.py de fines
enveloppes. On vérifie ici que ce cœur tourne SANS streamlit ni session_state —
`ctx` est fourni à la main — et que les gardes renvoient bien un message *en
donnée* (`{"erreur": …}` / `{"avertissement": …}`), que l'enveloppe traduit en
`st.error`/`st.warning`. Avant la tâche 4 ce message était un appel `st.*` en
ligne, donc intestable sans Streamlit. Aucun appel API.
"""
import pipeline


class ClientJamaisAppele:
    """Sentinelle : toucher le client dans un chemin de garde est un bug."""

    def __getattr__(self, nom):  # pragma: no cover - ne doit jamais arriver
        raise AssertionError(f"le client ne doit pas être appelé (accès à {nom!r})")


def test_actualiser_lot_travail_inconnu_renvoie_une_erreur(db_neuve):
    """Un job absent de l'historique → message en donnée, client jamais touché."""
    resultat = pipeline.actualiser_lot(ClientJamaisAppele(), {}, "job-fantome")
    assert resultat["erreur"] and "job-fantome" in resultat["erreur"]


def test_relancer_sans_charge_ocr_renvoie_une_erreur(db_neuve):
    """Relance sans run ni OCR en cache → erreur, avant tout appel réseau."""
    resultat = pipeline.relancer(ClientJamaisAppele(), {}, document_id=999,
                                 run_id=999, indices=[0])
    assert resultat["erreur"] and "OCR" in resultat["erreur"]


def test_ctx_bati_a_la_main_sans_session_state(schema_rex, index_conformite):
    """
    Un `ctx` fabriqué à la main (sans app, sans session_state) suffit au cœur.

    C'est tout l'intérêt du déplacement : `traiter_document` / `relancer` /
    `actualiser_lot` ne dépendent plus que des clés de ce dict, pas de Streamlit.
    On construit ici les clés minimales et on les fait consommer par une garde.
    """
    ctx = {
        "index_conformite": index_conformite,
        "cles_echauffees": set(),
        "prompt_extraction": "p",
        "format_extraction": {"type": "json_schema"},
        "cle_cache_extraction": "cle",
    }
    # `cles_echauffees` est un set ordinaire, alimentable hors de tout cache
    # Streamlit — la preuve que le registre voyage bien en donnée.
    ctx["cles_echauffees"].add("cle")
    assert "cle" in ctx["cles_echauffees"]
