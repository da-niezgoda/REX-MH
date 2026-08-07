#!/usr/bin/env python
"""
Test de GROUPE : segmentation des faux recueils de `tests/corpus/`.

Rejoue la segmentation (énumérer→vérifier→raffiner, telle qu'en production) sur
chaque corpus synthétique et note le découpage avec le MÊME scoreur pur que
`eval_rex` (`noter_decoupage`). Aucun OCR, aucune extraction : seulement les
appels de segmentation, sur `mistral-small-latest`.

La barre critique client est « perdues == 0 » (aucun projet manquant) ET toutes
les fiches couvertes. Les fantômes (sommaire/carte pris pour une fiche) et les
fragments coûtent un appel d'extraction mais ne perdent rien — signalés, non
bloquants.

    .venv/bin/python tests/eval_corpus.py                 # groupe live
    .venv/bin/python tests/eval_corpus.py --resume out.json
"""
import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

CORPUS = RACINE / "tests/corpus"


def _ctx():
    from app import (MODEL_SEGMENTATION, json_schema_format, load_prompt,
                     load_schema)
    import pipeline

    schema = load_schema("REXlist.schema.json")
    prompt = load_prompt("listPrompt.md", schema)
    schema_check = load_schema("REXcheck.schema.json")
    prompt_check = load_prompt("verifyPrompt.md", schema_check)
    if not prompt or not prompt_check:
        sys.exit("listPrompt.md / verifyPrompt.md ou leurs schémas illisibles")
    return {
        "prompt_segmentation": prompt,
        "format_segmentation": json_schema_format("rex_liste_projets", schema),
        "cle_cache_segmentation": pipeline.cle_cache_prompt(
            "segmentation", prompt, MODEL_SEGMENTATION),
        "prompt_verification": prompt_check,
        "format_verification": json_schema_format("rex_verification", schema_check),
        "cle_cache_verification": pipeline.cle_cache_prompt(
            "verification", prompt_check, MODEL_SEGMENTATION),
    }


def _valides(segments, ocr):
    import pipeline
    travaux, echecs, _ = pipeline.preparer_segments(segments, ocr)
    return [segments[t["index"]] for t in travaux], echecs


def main():
    from dotenv import load_dotenv
    load_dotenv(RACINE / ".env")

    import pipeline
    from app import get_api_key
    from eval_rex import noter_decoupage

    cle = get_api_key()
    if not cle:
        sys.exit("MISTRAL_API_KEY absent (.env ou variable d'environnement)")
    client = pipeline.construire_client(cle)
    ctx = _ctx()

    fichiers = sorted(CORPUS.glob("corpus_*.json"))
    if not fichiers:
        sys.exit(f"aucun corpus dans {CORPUS} — lancer tests/corpus/_generateur.py")

    print(f"\nTest de groupe — {len(fichiers)} faux recueils, "
          f"segmentation énumérer→vérifier→raffiner\n")
    entete = (f"{'corpus':<16}{'REX':>4}{'pages':>6}"
              f"{'trouvés':>8}{'exacts':>7}{'couv.':>6}{'perdues':>8}"
              f"{'fantô.':>7}{'score':>7}  verdict")
    print(entete)
    print("-" * len(entete))

    resume, sans_perte, propres, notes = [], 0, 0, []
    for f in fichiers:
        data = json.loads(f.read_text(encoding="utf-8"))
        ocr, verite = data["ocr"], data["verite"]
        segments, _, _ = pipeline._segmenter(client, ocr, ctx)
        retenus, _ = _valides(segments, ocr)
        note = noter_decoupage(retenus, verite)
        notes.append(note["score"])

        ok = note["perdues"] == 0 and note["couvertes"] == note["attendues"]
        propre = ok and len(note["fantomes"]) == 0 and note["obtenus"] == note["attendues"]
        sans_perte += ok
        propres += propre
        verdict = "PROPRE" if propre else ("OK (sans perte)" if ok else "ÉCHEC")

        print(f"{data['nom']:<16}{data['rex']:>4}{verite['nb_pages']:>6}"
              f"{note['obtenus']:>8}{note['exactes']:>7}{note['couvertes']:>6}"
              f"{note['perdues']:>8}{len(note['fantomes']):>7}"
              f"{note['score']:>7.3f}  {verdict}")

        resume.append({
            "nom": data["nom"], "rex": data["rex"], "pages": verite["nb_pages"],
            "trouves": note["obtenus"],
            "exacts": note["exactes"], "couvertes": note["couvertes"],
            "perdues": note["perdues"], "fantomes": len(note["fantomes"]),
            "fragments": note["fragments"], "score": round(note["score"], 4),
            "sans_perte": bool(ok), "propre": bool(propre),
        })

    moyenne = sum(notes) / len(notes) if notes else 0.0
    print("-" * len(entete))
    print(f"\n  {sans_perte}/{len(fichiers)} sans aucune perte de projet · "
          f"{propres}/{len(fichiers)} découpages propres · "
          f"note moyenne {moyenne:.3f}")
    if sans_perte < len(fichiers):
        print("  ÉCHEC : au moins un projet réel a disparu — la seule issue "
              "inacceptable.")

    if "--resume" in sys.argv:
        chemin = Path(sys.argv[sys.argv.index("--resume") + 1])
        chemin.write_text(json.dumps(
            {"corpus": resume, "sans_perte": sans_perte, "propres": propres,
             "total": len(fichiers), "note_moyenne": round(moyenne, 4)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  résumé écrit dans {chemin}")
    print()


if __name__ == "__main__":
    main()
