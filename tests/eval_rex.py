#!/usr/bin/env python
"""
Mesure de la qualité d'un run REX. Un script, pas un test : il s'exécute à la
main avant et après une modification de prompt ou de schéma, et il imprime un
rapport comparable d'une fois sur l'autre.

Trois composantes, toutes mesurables sans étiquetage expert :

  · DÉCOUPAGE   — comparé à `tests/fixtures/verite-18p.json`. La seule qui ait
                  demandé une vérité terrain, et elle tient en quelques numéros
                  de page.
  · CONFORMITÉ  — part des fiches en `validation_status = 'conforme'`. C'est
                  littéralement la barre « zéro recalage = propre », donc la
                  mesure est un COUNT, sans arithmétique dérivée.
  · REMPLISSAGE — feuilles non vides sur 33. Garde-fou indispensable : un prompt
                  qui régresse en renvoyant « » partout serait par ailleurs noté
                  parfaitement conforme.

La métrique est **versionnée**, jamais renormalisée. La correspondance par champ
demande les experts du client ; quand ces étiquettes arriveront elles feront un
`v2`, et on ne comparera jamais un v1 à un v2.

Le découpage est rejoué DEPUIS LA CHARGE OCR FIGÉE : un seul appel
`mistral-small-latest`, aucun OCR, aucun téléversement, aucune extraction. C'est
ce qui rend une itération de prompt assez bon marché pour être faite dix fois.

Usage :
    .venv/bin/python tests/eval_rex.py                      # découpage, 1 appel live
    .venv/bin/python tests/eval_rex.py --rejouer <fichier>  # découpage, 0 appel
    .venv/bin/python tests/eval_rex.py --run-id 12          # + conformité et remplissage
    .venv/bin/python tests/eval_rex.py --journal            # ajoute la note au journal
"""
import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

FIXTURE = RACINE / "tests/fixtures/ocr-18p.json"
VERITE = RACINE / "tests/fixtures/verite-18p.json"
SORTIES = RACINE / "tests/evals"
JOURNAL = SORTIES / "journal.jsonl"

SEUIL_COUVERTURE = 0.5

# Nom de la métrique selon ce qui a pu être mesuré. Deux noms distincts, pour
# qu'une note partielle ne soit jamais comparée à une note complète par accident.
METRIQUE_DECOUPAGE = "decoupage_v1"
METRIQUE_COMPLETE = "score_rex_v1"

# Poids figés pour la v1. La correspondance par champ manque, mais la renormaliser
# à mesure que des étiquettes arrivent rendrait les runs incomparables EN SILENCE
# — d'où une version explicite plutôt qu'une pondération mouvante.
POIDS = {"decoupage": 0.45, "conformite": 0.35, "remplissage": 0.20}


# --- Notation : fonctions pures, sans API ni fichier ------------------------


def _pages(debut, fin):
    return set(range(debut, fin + 1))


def _iou(a, b):
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def pages_hors_projet(verite):
    """Ensemble aplati des pages qui n'appartiennent à aucune fiche."""
    return {p for groupe in verite["hors_projet"] for p in groupe["pages"]}


_VIDES = {"de", "du", "des", "la", "le", "les", "l", "d", "et", "en", "a", "au",
          "aux", "entre", "sur", "un", "une"}


def _mots(texte):
    """Mots significatifs, minuscules et sans accents, pour comparer deux titres."""
    import re
    import unicodedata
    plat = unicodedata.normalize("NFKD", texte or "")
    plat = "".join(c for c in plat if not unicodedata.combining(c)).casefold()
    return {m for m in re.split(r"[^0-9a-z]+", plat) if m and m not in _VIDES}


def recouvrement_titre(obtenu, attendu):
    """
    Part des mots significatifs du titre attendu que le titre obtenu reprend.

    Volontairement hors du score : un titre est du texte libre et une petite
    divergence n'est pas un défaut. Mais un recouvrement quasi nul l'est, et
    c'est le seul moyen de voir la contamination observée en pratique — le
    modèle avait donné aux pages 10-14 le titre du spécimen de la page 4, avec
    des bornes de pages pourtant parfaites. Le score seul ne l'aurait pas vu.
    """
    cible = _mots(attendu)
    return len(cible & _mots(obtenu)) / len(cible) if cible else 1.0


def noter_decoupage(segments, verite):
    """
    Compare un découpage à la vérité terrain. Fonction pure — c'est elle que la
    suite de tests appellera, pas le chemin réseau.

    `segments` : liste de dicts {Titre, PageDebut, PageFin} telle que le modèle
    la renvoie. Les segments invalides doivent avoir été écartés en amont.

    Vocabulaire, choisi pour que l'issue grave soit impossible à manquer :
      · perdues   — fiche réelle qu'AUCUN segment ne recouvre. Seule issue fatale :
                    un projet manquant dans l'Excel du client est pire que des
                    lignes parasites, et beaucoup plus difficile à remarquer.
      · fantomes  — segment qui ne recouvre aucune fiche réelle (l'intro, un
                    tableau de sommaire, la carte). Chaque fantôme = un appel
                    d'extraction gaspillé et une ligne à jeter.
      · fragments — segments surnuméraires sur une même fiche (sur-découpage
                    *interne*, qui coûte aussi un appel mais ne perd rien).
    """
    attendues = verite["fiches_attendues"]
    detail, fantomes_vus = [], set(range(len(segments)))

    for fiche in attendues:
        cible = _pages(fiche["page_debut"], fiche["page_fin"])
        chevauchants = [
            (i, s) for i, s in enumerate(segments)
            if _pages(s["PageDebut"], s["PageFin"]) & cible
        ]
        fantomes_vus -= {i for i, _ in chevauchants}
        if not chevauchants:
            detail.append({"reference": fiche["reference"], "iou": 0.0,
                           "exacte": False, "couverte": False, "perdue": True,
                           "segments": 0, "obtenu": None})
            continue
        meilleur_i, meilleur = max(
            chevauchants, key=lambda c: _iou(_pages(c[1]["PageDebut"], c[1]["PageFin"]), cible)
        )
        iou = _iou(_pages(meilleur["PageDebut"], meilleur["PageFin"]), cible)
        detail.append({
            "reference": fiche["reference"],
            "iou": iou,
            "exacte": (meilleur["PageDebut"] == fiche["page_debut"]
                       and meilleur["PageFin"] == fiche["page_fin"]),
            "couverte": iou >= SEUIL_COUVERTURE,
            "perdue": False,
            "segments": len(chevauchants),
            "obtenu": [meilleur["PageDebut"], meilleur["PageFin"]],
            "indice": meilleur_i,
            "titre_obtenu": meilleur.get("Titre", ""),
            "titre_recouvrement": recouvrement_titre(
                meilleur.get("Titre", ""), fiche["titre_indicatif"]),
        })

    fantomes = [
        {"indice": i, "titre": segments[i].get("Titre", ""),
         "pages": [segments[i]["PageDebut"], segments[i]["PageFin"]]}
        for i in sorted(fantomes_vus)
    ]
    fragments = sum(max(0, d["segments"] - 1) for d in detail)
    obtenus = len(segments)

    couvertes = sum(1 for d in detail if d["couverte"])
    rappel = couvertes / len(attendues) if attendues else 0.0
    precision = (obtenus - len(fantomes)) / obtenus if obtenus else 0.0
    f1 = (2 * precision * rappel / (precision + rappel)) if (precision + rappel) else 0.0
    iou_moyen = sum(d["iou"] for d in detail) / len(detail) if detail else 0.0

    return {
        "attendues": len(attendues),
        "obtenus": obtenus,
        "exactes": sum(1 for d in detail if d["exacte"]),
        "couvertes": couvertes,
        "perdues": sum(1 for d in detail if d["perdue"]),
        "fantomes": fantomes,
        "fragments": fragments,
        "precision": precision,
        "rappel": rappel,
        "f1": f1,
        "iou_moyen": iou_moyen,
        # La note pénalise à la fois les fantômes (précision) et les fiches
        # manquées (rappel), puis module par la justesse des bornes.
        "score": f1 * iou_moyen,
        "detail": detail,
    }


def afficher_decoupage(note, verite):
    a, o = note["attendues"], note["obtenus"]
    print(f"\n  {a} attendue(s) / {o} obtenu(s) · "
          f"{len(note['fantomes'])} fantôme(s) · {note['perdues']} perdue(s)")
    if note["fragments"]:
        print(f"  {note['fragments']} segment(s) surnuméraire(s) sur une fiche réelle")

    print("\n  fiches réelles")
    for d in note["detail"]:
        attendu = next(f for f in verite["fiches_attendues"]
                       if f["reference"] == d["reference"])
        borne = f"p.{attendu['page_debut']}-{attendu['page_fin']}"
        if d["perdue"]:
            etat = "PERDUE — aucun segment ne la recouvre"
        else:
            obtenu = f"p.{d['obtenu'][0]}-{d['obtenu'][1]}"
            marque = "exacte" if d["exacte"] else f"IoU {d['iou']:.2f}"
            etat = f"{obtenu} ({marque})"
        print(f"    · {d['reference']:<10} {borne:<10} -> {etat}")
        if not d["perdue"] and d["titre_recouvrement"] < 0.5:
            print(f"        titre douteux ({d['titre_recouvrement']:.0%} des mots "
                  f"attendus) : {d['titre_obtenu'][:60]!r}")
            print(f"        attendu plutôt : {attendu['titre_indicatif'][:60]!r}")

    if note["fantomes"]:
        print("\n  fantômes (appels d'extraction gaspillés)")
        hors = pages_hors_projet(verite)
        for f in note["fantomes"]:
            pages = set(range(f["pages"][0], f["pages"][1] + 1))
            motifs = {g["motif"] for g in verite["hors_projet"]
                      if set(g["pages"]) & pages}
            dedans = "toutes hors projet" if pages <= hors else "partiellement hors projet"
            print(f"    · p.{f['pages'][0]}-{f['pages'][1]}  {f['titre'][:44]:<44} [{dedans}]")
            for m in sorted(motifs):
                print(f"        {m}")

    print(f"\n  précision {note['precision']:.2f} · rappel {note['rappel']:.2f} · "
          f"F1 {note['f1']:.2f} · IoU moyen {note['iou_moyen']:.2f}")
    print(f"  note de découpage : {note['score']:.3f}")
    if note["perdues"]:
        print("  ÉCHEC : une fiche réelle a disparu. C'est la seule issue "
              "inacceptable — corriger avant toute autre mesure.")


# --- Chemin live : un appel de segmentation depuis la charge figée ----------


def _decouper_en_direct(charge, verite):
    """Un appel `mistral-small-latest`. Renvoie (segments, usage, meta)."""
    from mistralai.client import Mistral

    import pipeline
    from app import (MODEL_SEGMENTATION, get_api_key, json_schema_format,
                     load_prompt, load_schema)

    cle_api = get_api_key()
    if not cle_api:
        sys.exit("MISTRAL_API_KEY absent (.env ou variable d'environnement)")

    schema = load_schema("REXlist.schema.json")
    prompt = load_prompt("listPrompt.md", schema)
    if not prompt:
        sys.exit("listPrompt.md ou REXlist.schema.json illisible")
    cle_cache = pipeline.cle_cache_prompt("segmentation", prompt, MODEL_SEGMENTATION)

    client = Mistral(api_key=cle_api)
    reponse = client.chat.complete(
        **pipeline.construire_requete_chat(
            prompt, pipeline.clean_document(charge), modele=MODEL_SEGMENTATION,
            response_format=json_schema_format("rex_liste_projets", schema),
            prompt_cache_key=cle_cache,
        ),
        timeout_ms=pipeline.TIMEOUT_SEGMENTATION_MS,
        retries=pipeline.RETRY_SEGMENTATION,
    )
    brut = json.loads(reponse.choices[0].message.content)
    usage = pipeline.usage_depuis_reponse(reponse)
    meta = {
        "modele_demande": MODEL_SEGMENTATION,
        "modele_servi": pipeline._resolved_model(reponse, MODEL_SEGMENTATION),
        "prompt_sha256": pipeline.empreinte(prompt),
        "cle_cache_prompt": cle_cache,
        "longueur_prompt": len(prompt),
    }
    return brut, usage, meta


def feuilles_du_schema(schema, prefixe=""):
    """Chemins « Section/champ » de toutes les feuilles du schéma."""
    chemins = []
    for nom, noeud in (schema.get("properties") or {}).items():
        chemin = f"{prefixe}{nom}"
        if noeud.get("type") == "object":
            chemins.extend(feuilles_du_schema(noeud, prefixe=f"{chemin}/"))
        else:
            chemins.append(chemin)
    return chemins


def noter_conformite(verdicts):
    """
    Part des fiches déclarées `conforme`. Fonction pure.

    « corrigé » ne compte PAS comme propre : la barre est zéro recalage, et une
    fiche corrigée signale que le prompt ou le schéma perd du terrain.
    """
    verdicts = [v for v in verdicts if v]
    if not verdicts:
        return None
    comptes = {}
    for verdict in verdicts:
        comptes[verdict] = comptes.get(verdict, 0) + 1
    return {
        "fiches": len(verdicts),
        "comptes": comptes,
        "score": comptes.get("conforme", 0) / len(verdicts),
    }


def noter_remplissage(fiches, schema):
    """
    Feuilles non vides sur le total. Fonction pure.

    Attrape ce que la conformité ne voit pas : un prompt qui régresse en
    renvoyant « » partout resterait parfaitement conforme au schéma.
    """
    chemins = feuilles_du_schema(schema)
    if not fiches or not chemins:
        return None
    remplies = total = 0
    for fiche in fiches:
        for chemin in chemins:
            section, champ = chemin.split("/", 1)
            valeur = (fiche.get(section) or {}).get(champ)
            total += 1
            if isinstance(valeur, list):
                remplies += 1 if [v for v in valeur if str(v).strip()] else 0
            elif str(valeur or "").strip():
                remplies += 1
    return {"feuilles": len(chemins), "score": remplies / total if total else 0.0}


def score_composite(decoupage, conformite_, remplissage):
    """
    La note unique, ou None si une composante manque.

    Refuser de composer une note incomplète est délibéré : un score amputé d'une
    composante ressemble à une régression alors qu'il ne mesure pas la même chose.
    """
    composantes = {"decoupage": decoupage, "conformite": conformite_,
                   "remplissage": remplissage}
    if any(c is None for c in composantes.values()):
        return None
    return 100 * sum(POIDS[nom] * c["score"] for nom, c in composantes.items())


def lire_run(run_id):
    """
    (verdicts, fiches, méta du run) depuis SQLite. Aucun appel API.

    `runs` porte déjà les empreintes de prompt et de schéma et les versions de
    modèle, donc « regrouper sur l'empreinte » ne demande aucune colonne de plus.
    """
    import os

    import store

    store.init_db(os.environ.get("REX_DB_PATH") or store.DEFAULT_DB_PATH)
    run = store.get_run(run_id)
    if run is None:
        sys.exit(f"Run {run_id} introuvable dans {store.db_path()}")
    lignes = store.list_fiches(run_id, status="ok")
    verdicts = [l["validation_status"] for l in lignes]
    fiches = [json.loads(l["data_json"]) for l in lignes if l["data_json"]]
    return verdicts, fiches, run


def ligne_journal(note, meta, usage, verite, *, conformite_=None,
                  remplissage=None, composite=None, run=None):
    """
    Ligne de journal : QUE des nombres et des empreintes, aucun titre de projet.
    C'est ce qui la rend versionnable — un changement de prompt montre alors son
    delta de score directement dans le diff.
    """
    ligne = {
        "metrique": METRIQUE_COMPLETE if composite is not None else METRIQUE_DECOUPAGE,
        "score": round(composite, 2) if composite is not None else round(note["score"], 4),
        "composantes": {
            "decoupage": round(note["score"], 4),
            "conformite": round(conformite_["score"], 4) if conformite_ else None,
            "remplissage": round(remplissage["score"], 4) if remplissage else None,
        },
        "document_sha256": verite["document_sha256"][:16],
        "prompt_sha256": (meta or {}).get("prompt_sha256", "")[:16],
        "modele": (meta or {}).get("modele_servi"),
        "attendues": note["attendues"],
        "obtenus": note["obtenus"],
        "exactes": note["exactes"],
        "perdues": note["perdues"],
        "fantomes": len(note["fantomes"]),
        "fragments": note["fragments"],
        "precision": round(note["precision"], 4),
        "rappel": round(note["rappel"], 4),
        "iou_moyen": round(note["iou_moyen"], 4),
        "titre_recouvrement_min": round(
            min((d["titre_recouvrement"] for d in note["detail"]
                 if not d["perdue"]), default=0.0), 4),
        "jetons_prompt": (usage or {}).get("prompt_tokens"),
        "jetons_caches": (usage or {}).get("cached_tokens"),
        # Couverture de la vérité terrain : dire ce qui n'est PAS mesuré évite de
        # lire la note comme si elle couvrait tout.
        "couverture_verite": {"fiches": len(verite["fiches_attendues"]),
                              "champs": len(verite.get("champs") or {})},
    }
    if run is not None:
        ligne.update({
            "run_id": run["id"],
            "modele_extraction": run["model_extraction"],
            "prompt_extraction_sha256": (run["prompt_extraction_sha256"] or "")[:16],
            "schema_rex_sha256": (run["schema_rex_sha256"] or "")[:16],
        })
    if conformite_:
        ligne["verdicts"] = conformite_["comptes"]
    return ligne


def _valides(brut, charge):
    """Segments retenus par `preparer_segments`, plus ce qu'il a refusé."""
    import pipeline

    segments = brut.get("Liste", [])
    travaux, echecs, avertissements = pipeline.preparer_segments(segments, charge)
    retenus = [segments[t["index"]] for t in travaux]
    return retenus, echecs, avertissements


def main():
    arguments = sys.argv[1:]
    if not FIXTURE.exists():
        sys.exit(f"Charge OCR figée absente : {FIXTURE}")
    charge = json.loads(FIXTURE.read_text(encoding="utf-8"))
    verite = json.loads(VERITE.read_text(encoding="utf-8"))

    if "--rejouer" in arguments:
        chemin = Path(arguments[arguments.index("--rejouer") + 1])
        sauve = json.loads(chemin.read_text(encoding="utf-8"))
        brut, usage, meta = sauve["brut"], sauve.get("usage"), sauve.get("meta", {})
        print(f"\n[1/2] Découpage rejoué depuis {chemin} — aucun appel API")
    else:
        print(f"\n[1/2] Découpage en direct depuis {FIXTURE.name} "
              f"({verite['nb_pages']} pages) — 1 appel de segmentation")
        brut, usage, meta = _decouper_en_direct(charge, verite)
        SORTIES.mkdir(parents=True, exist_ok=True)
        chemin = SORTIES / f"seg-{meta['prompt_sha256'][:16]}.json"
        chemin.write_text(json.dumps({"brut": brut, "usage": usage, "meta": meta},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"      sortie brute sauvée dans {chemin.relative_to(RACINE)}")

    if meta:
        print(f"      modèle servi : {meta.get('modele_servi')} · "
              f"prompt {meta.get('longueur_prompt')} car. "
              f"(sha {meta.get('prompt_sha256', '')[:16]})")
    if usage:
        print(f"      jetons prompt {usage['prompt_tokens']} · "
              f"cache {usage['cached_tokens']} · "
              f"génération {usage['completion_tokens']}")

    segments = brut.get("Liste", [])
    retenus, echecs, avertissements = _valides(brut, charge)
    print(f"      {len(segments)} segment(s) renvoyé(s), {len(retenus)} retenu(s) "
          f"après validation")
    for e in echecs:
        print(f"        ! {e['titre'][:40]} — {e['error']}")
    for a in avertissements:
        print(f"        ~ {a}")
    if "PagesHorsProjet" in brut:
        print(f"      PagesHorsProjet annoncées : {brut['PagesHorsProjet']}")

    print("\n[2/2] Notation")
    note = noter_decoupage(retenus, verite)
    afficher_decoupage(note, verite)

    note_conformite = note_remplissage = run = None
    if "--run-id" in arguments:
        run_id = int(arguments[arguments.index("--run-id") + 1])
        schema = json.loads((RACINE / "REX.schema.json").read_text(encoding="utf-8"))
        verdicts, fiches, run = lire_run(run_id)
        note_conformite = noter_conformite(verdicts)
        note_remplissage = noter_remplissage(fiches, schema)
        print(f"\n  run {run_id} · {len(fiches)} fiche(s) · "
              f"modèle {run['model_extraction']}")
        if note_conformite is None:
            print("  conformité : aucun verdict enregistré (run antérieur à la "
                  "tâche 3 ?)")
        else:
            detail = ", ".join(f"{n} {statut}" for statut, n
                               in sorted(note_conformite["comptes"].items()))
            print(f"  conformité  : {note_conformite['score']:.0%}  ({detail})")
        if note_remplissage is not None:
            print(f"  remplissage : {note_remplissage['score']:.0%}  "
                  f"({note_remplissage['feuilles']} feuilles par fiche)")

    composite = score_composite(note, note_conformite, note_remplissage)
    if composite is None:
        print(f"\n  {METRIQUE_DECOUPAGE} : {note['score']:.3f}")
        print("  (note composite indisponible : passez --run-id pour la conformité "
              "et le remplissage)")
    else:
        print(f"\n  {METRIQUE_COMPLETE} : {composite:.1f}/100")
        print("  " + " · ".join(
            f"{nom} {POIDS[nom]:.0%}×{c['score']:.2f}"
            for nom, c in (("decoupage", note), ("conformite", note_conformite),
                           ("remplissage", note_remplissage))))
    champs = len(verite.get("champs") or {})
    if not champs:
        print("  couverture de la vérité : bornes de pages seulement, aucun champ "
              "étiqueté — la correspondance par champ attend les experts.")

    if "--journal" in arguments:
        SORTIES.mkdir(parents=True, exist_ok=True)
        ligne = ligne_journal(note, meta, usage, verite,
                             conformite_=note_conformite,
                             remplissage=note_remplissage,
                             composite=composite, run=run)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
        print(f"  ajouté à {JOURNAL.relative_to(RACINE)}")
    print()


if __name__ == "__main__":
    main()
