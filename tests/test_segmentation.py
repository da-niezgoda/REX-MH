"""
Segmentation par énumération + boucle vérifier→raffiner — la logique qui rattrape
la paresse du modèle sur les longs recueils. Aucun appel API : toutes les fonctions
sous test sont pures ou tournent contre un faux client scripté.

Helpers purs (`_trous_interieurs`, `_audit_propre`, `_ajouter_manquants`,
`_assainir_critique`) et intégration de `_segmenter` contre un faux client scripté
qui joue énumérations et audits dans l'ordre. C'est la logique qu'aucun test
n'exerçait quand le recueil de 129 pages a rendu 10 fiches au lieu de ~26 — et que
le test de groupe `eval_corpus.py` mesure en live.
"""
import json

import pytest

import pipeline as p


# --- Fabriques de test -------------------------------------------------------


def seg(debut, fin, titre="x"):
    return {"PageDebut": debut, "PageFin": fin, "Titre": titre, "Motif": "m"}


def bornes(segments):
    return [(s["PageDebut"], s["PageFin"]) for s in segments]


# --- Boucle énumérer → vérifier → raffiner : helpers purs -------------------


@pytest.mark.parametrize("liste, nb_pages, attendu", [
    ([], 5, []),                                    # aucune fiche → aucun indice
    ([seg(1, 5)], 5, []),                            # tout couvert
    ([seg(2, 3)], 5, []),                            # tête (1) et queue (4,5) écartées
    ([seg(1, 2), seg(4, 5)], 6, [3]),               # trou intérieur ; queue (6) écartée
    ([seg(3, 4), seg(1, 2)], 5, []),                # entrées non triées, contiguës
    ([seg(1, 3), seg(2, 5)], 5, []),                # plages recouvrantes → union
    ([seg(10, 53), seg(76, 129)], 129, list(range(54, 76))),   # le vrai trou 54-75
    ([{"PageDebut": None, "PageFin": 3}], 4, []),   # bornes inexploitables ignorées
])
def test_trous_interieurs(liste, nb_pages, attendu):
    assert p._trous_interieurs(liste, nb_pages) == attendu


def test_trous_interieurs_ecarte_le_preambule_de_tete():
    """
    Régression 129 pages : les pages 1-9 (intro/sommaire/carte) devant la 1re
    fiche NE sont PAS offertes — c'est ce qui poussait le vérificateur à les
    promouvoir en 3 fiches fantômes. Seul le trou intérieur 54-75 ressort.
    """
    liste = [seg(10, 53, "début"), seg(76, 129, "fin")]
    assert p._trous_interieurs(liste, 129) == list(range(54, 76))


def test_trous_interieurs_borne_inversee_traitee_comme_plage():
    """Bornes inversées : réordonnées pour la couverture (seg(5,2) → 2-5), pas
    écartées — indice conservateur. Couvre {2-5, 8-9} → trou intérieur {6,7}."""
    assert p._trous_interieurs([seg(5, 2), seg(8, 9)], 10) == [6, 7]


@pytest.mark.parametrize("critique, propre", [
    (None, True),
    ({}, True),
    ({"manquants": [], "superflus": []}, True),
    ({"manquants": [{"page_debut": 3, "page_fin": 4}], "superflus": []}, False),
    ({"manquants": [], "superflus": [{"index": 1}]}, False),
])
def test_audit_propre(critique, propre):
    assert p._audit_propre(critique) is propre


def test_ajouter_manquants_ajoute_sans_jamais_retirer():
    """
    Le filet ajoute les manquants ET NE RETIRE JAMAIS un superflu — retirer
    perdrait un vrai projet. Régression corpus_2 (OCR réel) : une fiche réelle
    marquée superflue à tort ; la supprimer était la seule issue inacceptable.
    """
    liste = [seg(1, 3, "vrai"), seg(4, 4, "flag_a_tort"), seg(5, 8, "vrai2")]
    critique = {
        "superflus": [{"index": 1, "titre": "flag_a_tort"}],       # doit être IGNORÉ
        "manquants": [{"page_debut": 9, "page_fin": 12, "titre": "omis", "motif": "m"}],
    }
    resultat = p._ajouter_manquants(liste, critique)
    assert [s["Titre"] for s in resultat] == ["vrai", "flag_a_tort", "vrai2", "omis"]
    assert bornes(resultat) == [(1, 3), (4, 4), (5, 8), (9, 12)]


def test_ajouter_manquants_ignore_les_bornes_inexploitables():
    liste = [seg(1, 3, "vrai")]
    critique = {"superflus": [], "manquants": [
        {"page_debut": None, "page_fin": 5, "titre": "sans début", "motif": "m"},
        {"page_debut": 4, "page_fin": 6, "titre": "bon", "motif": "m"},
    ]}
    assert [s["Titre"] for s in p._ajouter_manquants(liste, critique)] == ["vrai", "bon"]


def test_ajouter_manquants_none_est_une_copie():
    liste = [seg(1, 3)]
    resultat = p._ajouter_manquants(liste, None)
    assert resultat == liste and resultat is not liste


def crit(manquants=(), superflus=()):
    return {"manquants": list(manquants), "superflus": list(superflus)}


def test_assainir_jette_un_dump_de_superflus():
    """Plus de la moitié des entrées en superflus = confusion → tous ignorés
    (le bug live : les 8 entrées correctes rendues en superflus)."""
    c = crit(superflus=[{"index": i, "titre": "x", "motif": "correcte"} for i in range(8)])
    assert p._assainir_critique(c, 8)["superflus"] == []


def test_assainir_garde_une_minorite_de_superflus():
    c = crit(superflus=[{"index": 0, "titre": "carte", "motif": "m"},
                        {"index": 3, "titre": "sommaire", "motif": "m"}])
    assert [s["index"] for s in p._assainir_critique(c, 8)["superflus"]] == [0, 3]


def test_assainir_ecarte_les_index_hors_bornes_et_non_entiers():
    c = crit(superflus=[{"index": 0}, {"index": 99}, {"index": True}, {"index": "1"}])
    # 0 valide ; 99 hors bornes ; True et "1" non entiers → seul 0 reste (1 ≤ 0.5×5).
    assert [s["index"] for s in p._assainir_critique(c, 5)["superflus"]] == [0]


def test_assainir_preserve_toujours_les_manquants():
    c = crit(manquants=[{"page_debut": 3, "page_fin": 4}],
             superflus=[{"index": i} for i in range(9)])       # dump ignoré
    resultat = p._assainir_critique(c, 9)
    assert len(resultat["manquants"]) == 1 and resultat["superflus"] == []


def test_assainir_none_reste_none():
    assert p._assainir_critique(None, 5) is None


# --- Intégration : la boucle _segmenter (faux client scripté) ---------------


def _reponse(contenu):
    """Réponse chat minimale, canard-typée comme le SDK (usage compris)."""
    usage = type("U", (), {"prompt_tokens": 1000, "completion_tokens": 100,
                           "total_tokens": 1100,
                           "prompt_tokens_details": {"cached_tokens": 0}})()
    message = type("M", (), {"content": contenu})()
    return type("R", (), {"choices": [type("C", (), {"message": message})()],
                          "usage": usage, "model": "mistral-small-2506"})()


class FauxClientBoucle:
    """
    Faux client scripté pour la boucle énumérer→vérifier→raffiner. Distingue les
    deux natures d'appel par la clé de cache de prompt (comme en production) :
    « enum » pour l'énumération, « verif » pour l'audit. Sert énumérations et
    audits dans l'ordre ; au-delà du script, répète le dernier élément (ce qui
    modélise un modèle qui refuse obstinément de se corriger).
    """

    def __init__(self, enumerations, audits):
        self.enumerations = list(enumerations)
        self.audits = list(audits)
        self.chat = self
        self.enum, self.verif = 0, 0
        self.contenus_enum, self.contenus_verif = [], []

    def complete(self, **kw):
        contenu = json.loads(kw["messages"][1]["content"])
        if kw.get("prompt_cache_key") == "verif":
            self.verif += 1
            self.contenus_verif.append(contenu)
            audit = self.audits[min(self.verif - 1, len(self.audits) - 1)]
            return _reponse(json.dumps(audit))
        self.enum += 1
        self.contenus_enum.append(contenu)
        liste = self.enumerations[min(self.enum - 1, len(self.enumerations) - 1)]
        return _reponse(json.dumps({"Liste": liste}))


CTX_BOUCLE = {
    "prompt_segmentation": "ENUM", "format_segmentation": {"type": "json_schema"},
    "cle_cache_segmentation": "enum",
    "prompt_verification": "VERIF", "format_verification": {"type": "json_schema"},
    "cle_cache_verification": "verif",
}


def _charge(nb_pages):
    return {"pages": [{"index": i, "markdown": f"page {i + 1}"} for i in range(nb_pages)]}


def test_boucle_audit_propre_du_premier_coup():
    """Énumération parfaite → l'audit ne signale rien → 1 énumération + 1 audit, fin."""
    complet = [seg(1, 5, "A"), seg(6, 10, "B")]
    client = FauxClientBoucle([complet], [{"manquants": [], "superflus": []}])
    segments, model, usage = p._segmenter(client, _charge(10), CTX_BOUCLE)
    assert client.enum == 1 and client.verif == 1
    assert bornes(segments) == [(1, 5), (6, 10)]
    assert usage["appels"] == 2 and model == "mistral-small-2506"


def test_boucle_un_raffinement_rattrape_une_omission():
    """
    1er passage : B est omis. L'audit le signale en manquant. Le 2e passage
    (avec révision injectée) rend la liste complète, et le 2e audit est propre.
    """
    partiel = [seg(1, 5, "A")]
    complet = [seg(1, 5, "A"), seg(6, 10, "B")]
    audits = [{"manquants": [{"page_debut": 6, "page_fin": 10,
                              "titre": "B", "motif": "site nommé"}], "superflus": []},
              {"manquants": [], "superflus": []}]
    client = FauxClientBoucle([partiel, complet], audits)
    segments, _, _ = p._segmenter(client, _charge(10), CTX_BOUCLE)
    assert client.enum == 2 and client.verif == 2
    assert bornes(segments) == [(1, 5), (6, 10)]
    # La révision injectée au 2e appel porte la liste précédente ET l'audit.
    rev = client.contenus_enum[1]["revision"]
    assert rev["liste_precedente"][0]["Titre"] == "A"
    assert rev["manquants"][0]["titre"] == "B"


def test_boucle_verification_recoit_les_trous_interieurs():
    """Le contenu d'audit porte l'indice (trous INTÉRIEURS seulement) et la liste
    candidate indexée. Tête et queue non couvertes ne sont pas offertes."""
    partiel = [seg(1, 5, "A"), seg(9, 10, "B")]    # trou intérieur 6-8, pas de tête/queue
    client = FauxClientBoucle([partiel], [{"manquants": [], "superflus": []}])
    p._segmenter(client, _charge(10), CTX_BOUCLE)
    assert client.contenus_verif[0]["pages_non_couvertes"] == [6, 7, 8]
    assert [e["index"] for e in client.contenus_verif[0]["liste_a_verifier"]] == [0, 1]


def test_boucle_filet_no_loss_force_le_dernier_audit():
    """
    Le modèle refuse obstinément d'ajouter B (chaque énumération le rate), mais
    l'audit le signale à chaque tour. Après MAX_ITER, le filet force l'ajout :
    aucun projet réel ne peut être perdu — la seule issue inacceptable.
    """
    partiel = [seg(1, 5, "A")]
    manquant = {"manquants": [{"page_debut": 6, "page_fin": 10,
                               "titre": "B", "motif": "site nommé"}], "superflus": []}
    client = FauxClientBoucle([partiel], [manquant])   # répétés au-delà du script
    segments, _, _ = p._segmenter(client, _charge(10), CTX_BOUCLE)
    assert client.enum == p.MAX_ITER_SEGMENTATION      # 1 initial + (MAX_ITER-1) raffinements
    assert client.verif == p.MAX_ITER_SEGMENTATION
    assert bornes(segments) == [(1, 5), (6, 10)], "B forcé par le filet no-loss"
    assert [s["Titre"] for s in segments] == ["A", "B"]


def test_boucle_retire_une_entree_superflue():
    """Un fantôme (carte) est repéré superflu ; le raffinement le retire."""
    avec_fantome = [seg(1, 5, "A"), seg(6, 6, "carte"), seg(7, 10, "B")]
    propre = [seg(1, 5, "A"), seg(7, 10, "B")]
    audits = [{"manquants": [], "superflus": [{"index": 1, "titre": "carte",
                                               "motif": "carte de localisation"}]},
              {"manquants": [], "superflus": []}]
    client = FauxClientBoucle([avec_fantome, propre], audits)
    segments, _, _ = p._segmenter(client, _charge(10), CTX_BOUCLE)
    assert [s["Titre"] for s in segments] == ["A", "B"]


def test_boucle_ignore_un_audit_qui_declare_tout_superflu():
    """
    Régression du bug live : l'audit a rendu les 8 entrées CORRECTES en superflus
    (motif « cette entrée est correcte »). Sans garde-fou le filet les supprimait
    toutes → 0 fiche. Le seuil SEUIL_SUPERFLUS jette ce dump : la liste énumérée
    survit intacte, et l'audit assaini étant propre, la boucle converge d'emblée.
    """
    complet = [seg(i * 2 + 1, i * 2 + 2, f"F{i}") for i in range(8)]
    tout_superflu = {"manquants": [],
                     "superflus": [{"index": i, "titre": f"F{i}",
                                    "motif": "cette entrée est correcte"} for i in range(8)]}
    client = FauxClientBoucle([complet], [tout_superflu])
    segments, _, _ = p._segmenter(client, _charge(16), CTX_BOUCLE)
    assert client.enum == 1 and client.verif == 1, "audit assaini propre → convergence immédiate"
    assert [s["Titre"] for s in segments] == [f"F{i}" for i in range(8)]


def test_boucle_ne_supprime_jamais_via_un_superflu_persistant():
    """
    Régression corpus_2 (OCR réel) : le vérificateur marque obstinément une fiche
    RÉELLE comme superflue à chaque tour (1 sur 3 — sous le seuil, donc l'assainissement
    la laisse passer). Le filet ne devant JAMAIS supprimer, la fiche survit : aucune
    perte. Avant le correctif, le filet de fin de boucle la supprimait → 1 projet perdu.
    """
    complet = [seg(1, 4, "A"), seg(5, 8, "B"), seg(9, 12, "C")]
    sup_persistant = {"manquants": [],
                      "superflus": [{"index": 1, "titre": "B", "motif": "à tort"}]}
    client = FauxClientBoucle([complet], [sup_persistant])     # jamais propre, jamais dropé
    segments, _, _ = p._segmenter(client, _charge(12), CTX_BOUCLE)
    assert [s["Titre"] for s in segments] == ["A", "B", "C"], "B réel ne doit pas être supprimé"
