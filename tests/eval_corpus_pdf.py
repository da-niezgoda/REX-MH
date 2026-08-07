#!/usr/bin/env python
"""
Test de GROUPE PDF : OCR RÉEL + segmentation des PDF de `tests/corpus/`.

Comme `eval_corpus.py`, mais part du PDF au lieu de la charge OCR injectée : chaque
recueil synthétique passe par le VRAI OCR Mistral (`mistral-ocr-latest`), puis la
même segmentation énumérer→vérifier→raffiner, notée par le même scoreur pur
`noter_decoupage`. C'est le bout-en-bout que le JSON ne fait pas — il révèle un
rendu PDF illisible ou un OCR qui déforme les titres, là où le JSON supposait une
charge parfaite.

L'OCR est mis en cache par empreinte du PDF dans le temp système : le premier run
paie l'OCR (~241 pages au total sur les 6), les suivants sont gratuits. La barre
reste « perdues == 0 » (aucun projet manquant) ET toutes les fiches couvertes.

    .venv/bin/python tests/eval_corpus_pdf.py                 # groupe live
    .venv/bin/python tests/eval_corpus_pdf.py corpus_1_3rex   # un seul (canari)
    .venv/bin/python tests/eval_corpus_pdf.py --resume out.json
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

CORPUS = RACINE / "tests/corpus"
CACHE = Path(tempfile.gettempdir()) / "rex_corpus_ocr"


def _ocr_pdf(client, pdf_bytes, filename):
    """
    OCR réel d'un PDF, mis en cache par empreinte dans le temp système. Renvoie
    (charge, depuis_cache) où charge est un dict — écrit une fois via
    model_dump_json, relu en dict brut, exactement comme le cache de production.
    """
    import pipeline
    CACHE.mkdir(parents=True, exist_ok=True)
    cle = CACHE / (hashlib.sha256(pdf_bytes).hexdigest()[:16] + ".json")
    if cle.exists():
        return json.loads(cle.read_text(encoding="utf-8")), True

    televerse = client.files.upload(
        file={"file_name": filename, "content": pdf_bytes}, purpose="ocr",
        timeout_ms=pipeline.TIMEOUT_UPLOAD_MS, retries=pipeline.RETRY_UPLOAD)
    try:
        signed = client.files.get_signed_url(file_id=televerse.id)
        ocr = client.ocr.process(
            model=pipeline.MODEL_OCR,
            document={"type": "document_url", "document_url": signed.url},
            timeout_ms=pipeline.TIMEOUT_OCR_MS, retries=pipeline.RETRY_OCR,
            **pipeline.OCR_PARAMS)
    finally:
        try:
            client.files.delete(file_id=televerse.id)
        except Exception:
            pass
    cle.write_text(ocr.model_dump_json(), encoding="utf-8")
    return json.loads(ocr.model_dump_json()), False


def main():
    from dotenv import load_dotenv
    load_dotenv(RACINE / ".env")

    import pipeline
    from app import get_api_key
    from eval_corpus import _ctx, _valides
    from eval_rex import noter_decoupage

    api = get_api_key()
    if not api:
        sys.exit("MISTRAL_API_KEY absent (.env ou variable d'environnement)")
    client = pipeline.construire_client(api)
    ctx = _ctx()

    args, cibles, i = sys.argv[1:], [], 0
    while i < len(args):
        if args[i] == "--resume":      # « --resume CHEMIN » : sauter le flag ET sa valeur,
            i += 2                     # sinon le chemin serait pris pour un nom de corpus
            continue
        if args[i].startswith("--"):
            i += 1
            continue
        cibles.append(args[i])
        i += 1
    pdfs = sorted(CORPUS.glob("corpus_*.pdf"))
    if cibles:
        pdfs = [p for p in pdfs if p.stem in cibles]
    if not pdfs:
        sys.exit(f"aucun PDF dans {CORPUS} — lancer tests/corpus/_generateur.py")

    print(f"\nTest de groupe PDF — {len(pdfs)} recueils, OCR réel + segmentation\n")
    entete = (f"{'corpus':<16}{'REX':>4}{'pages':>6}{'ocr':>5}"
              f"{'trouvés':>8}{'exacts':>7}{'couv.':>6}{'perdues':>8}"
              f"{'fantô.':>7}{'score':>7}  verdict")
    print(entete)
    print("-" * len(entete))

    resume, sans_perte, propres, notes = [], 0, 0, []
    for pdf in pdfs:
        data = json.loads((CORPUS / f"{pdf.stem}.json").read_text(encoding="utf-8"))
        verite = data["verite"]
        charge, depuis_cache = _ocr_pdf(client, pdf.read_bytes(), pdf.name)
        nb_ocr = pipeline.nombre_de_pages(charge)
        segments, _, _ = pipeline._segmenter(client, charge, ctx)
        retenus, _ = _valides(segments, charge)
        note = noter_decoupage(retenus, verite)
        notes.append(note["score"])

        ok = note["perdues"] == 0 and note["couvertes"] == note["attendues"]
        propre = ok and len(note["fantomes"]) == 0 and note["obtenus"] == note["attendues"]
        sans_perte += ok
        propres += propre
        verdict = "PROPRE" if propre else ("OK (sans perte)" if ok else "ÉCHEC")

        print(f"{data['nom']:<16}{data['rex']:>4}{verite['nb_pages']:>6}{nb_ocr:>5}"
              f"{note['obtenus']:>8}{note['exactes']:>7}{note['couvertes']:>6}"
              f"{note['perdues']:>8}{len(note['fantomes']):>7}"
              f"{note['score']:>7.3f}  {verdict}"
              f"{'' if depuis_cache else '  [OCR payé]'}")

        resume.append({
            "nom": data["nom"], "rex": data["rex"], "pages": verite["nb_pages"],
            "pages_ocr": nb_ocr, "trouves": note["obtenus"], "exacts": note["exactes"],
            "couvertes": note["couvertes"], "perdues": note["perdues"],
            "fantomes": len(note["fantomes"]), "fragments": note["fragments"],
            "score": round(note["score"], 4), "sans_perte": bool(ok), "propre": bool(propre),
        })

    moyenne = sum(notes) / len(notes) if notes else 0.0
    print("-" * len(entete))
    print(f"\n  {sans_perte}/{len(pdfs)} sans aucune perte de projet · "
          f"{propres}/{len(pdfs)} découpages propres · note moyenne {moyenne:.3f}")
    if sans_perte < len(pdfs):
        print("  ÉCHEC : au moins un projet réel a disparu — la seule issue "
              "inacceptable.")

    if "--resume" in sys.argv:
        chemin = Path(sys.argv[sys.argv.index("--resume") + 1])
        chemin.write_text(json.dumps(
            {"corpus": resume, "sans_perte": sans_perte, "propres": propres,
             "total": len(pdfs), "note_moyenne": round(moyenne, 4)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  résumé écrit dans {chemin}")
    print()


if __name__ == "__main__":
    main()
