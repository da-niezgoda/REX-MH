"""
Rendu de l'écran de résultats via AppTest — aucun appel API.

Régression ciblée : les cartes de synthèse (REX / conformité / jetons) étaient
liées au seul `process_uploaded_file`, si bien qu'au premier rerun — typiquement
APRÈS une relance de fiche — elles disparaissaient. On les rend désormais depuis
l'état rechargeable (`last_parsed_data` + totaux du run en base). Ce test injecte
exactement cet état d'après-relance et vérifie que les quatre cartes reviennent.

AppTest exécute `main()` en processus : la navigation, le thème et le CSS sont
réellement montés, mais on n'affirme que la STRUCTURE — pas des pixels.
"""
from streamlit.testing.v1 import AppTest

import store

SHA = "e" * 64


def _seed_run_recharge(db_temporaire):
    """Un run terminé à deux fiches conformes, puis rechargé comme après relance."""
    doc = store.get_or_create_document(SHA, "recueil.pdf")
    run_id, _ = store.start_run(doc, mode="rapide")
    store.add_run_usage(run_id, prompt_tokens=1234, completion_tokens=56)
    for seq in range(2):
        store.upsert_fiche(
            run_id, doc, seq, status="ok", titre=f"Fiche {seq}",
            page_debut=seq + 1, page_fin=seq + 1,
            data={"Presentation": {"Titre": f"Fiche {seq}"}},
            validation_status="conforme",
        )
    store.finish_run(run_id, status="termine")

    data = store.load_run_as_parsed_data(run_id)
    data["resultat"] = {"failures": []}   # exactement ce que pose _recharger_run
    return data


def test_cartes_persistent_sur_run_recharge(db_temporaire, contexte_app):
    data = _seed_run_recharge(db_temporaire)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.session_state["last_parsed_data"] = data
    at.run()

    assert len(at.exception) == 0, at.exception
    corps = " ".join(h.proto.body for h in at.get("html"))
    for libelle in ("REX extraites", "Conformité", "Jetons en entrée",
                    "Jetons en sortie"):
        assert libelle in corps, f"carte « {libelle} » absente après rechargement"
    # Valeurs calculées depuis l'état rechargeable, pas depuis un résultat éphémère.
    assert ">2<" in corps, "nombre de REX extraites"
    assert ">100%<" in corps, "taux de conformité (2/2 conformes)"
    assert ">1 234<" in corps, "jetons en entrée = total du run (espace fin insécable)"
    # L'écran de résultats est bien monté, avec une fiche par bouton.
    assert sum(1 for b in at.button if b.label.startswith("📄")) == 2


def test_bilan_frais_rendu_depuis_l_etat(db_temporaire, contexte_app):
    """
    Le bilan (message de succès + avertissements) est rendu par `page_traitement`
    DEPUIS l'état, pour survivre à la relance qui vide le dépôt juste après un
    traitement. Un run rechargé (resultat sans `statut`) ne le déclenche pas — c'est
    le cas du test précédent, qui pose `{"failures": []}` et ne doit pas planter.
    """
    data = _seed_run_recharge(db_temporaire)
    data["resultat"] = {
        "statut": "termine", "projects": [1, 2], "failures": [],
        "avertissements": ["Attention : page 5 rognée."], "conformite": {},
    }
    at = AppTest.from_file("app.py", default_timeout=30)
    at.session_state["last_parsed_data"] = data
    at.run()

    assert len(at.exception) == 0, at.exception
    textes = [e.value for e in at.success] + [e.value for e in at.warning]
    assert any("traité" in t for t in textes), "message de bilan absent"
    assert any("page 5 rognée" in t for t in textes), "avertissement non persistant"
