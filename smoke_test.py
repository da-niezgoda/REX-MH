#!/usr/bin/env python
"""
Vérification en direct de la chaîne Mistral après la montée de version (tâche 1).

Consomme volontairement peu de crédits : 1 OCR + 1 segmentation + 1 extraction
(la première fiche uniquement) sur l'extrait de 18 pages.

La clé est lue comme par l'application : .env en local, st.secrets en déploiement.

Usage :
    .venv/bin/python smoke_test.py
"""
import json
import sys
from collections import Counter
from pathlib import Path

from mistralai.client import Mistral

sys.path.insert(0, str(Path(__file__).parent))
from app import (  # noqa: E402
    MODEL_EXTRACTION,
    MODEL_OCR,
    MODEL_SEGMENTATION,
    RANDOM_SEED,
    clean_document,
    clean_pages,
    get_api_key,
    json_schema_format,
    load_prompt,
    load_schema,
)

PDF = Path("IFD_FICJOINT_0020373-1-18.pdf")


def api_key():
    key = get_api_key()
    if not key:
        sys.exit("MISTRAL_API_KEY absent (.env ou variable d'environnement)")
    return key


def main():
    if not PDF.exists():
        sys.exit(f"PDF de test introuvable : {PDF}")

    client = Mistral(api_key=api_key())
    rex_schema = load_schema("REX.schema.json")
    list_schema = load_schema("REXlist.schema.json")
    rex_prompt = load_prompt("REXPrompt.md", rex_schema)
    list_prompt = load_prompt("listPrompt.md", list_schema)

    print(f"\n[1/4] Upload de {PDF.name} ({PDF.stat().st_size / 1024:.0f} Ko)")
    uploaded = client.files.upload(
        file={"file_name": PDF.name, "content": PDF.read_bytes()}, purpose="ocr"
    )
    signed = client.files.get_signed_url(file_id=uploaded.id)
    print(f"      OK, file_id={uploaded.id}")

    print(f"\n[2/4] OCR avec {MODEL_OCR}")
    ocr = client.ocr.process(
        model=MODEL_OCR,
        document={"type": "document_url", "document_url": signed.url},
        include_image_base64=False,
        include_blocks=True,
        extract_header=True,
        extract_footer=True,
        confidence_scores_granularity="page",
        table_format="markdown",
    )
    pages = ocr.pages
    print(f"      {len(pages)} pages")
    blocks = [b for p in pages for b in (p.blocks or [])]
    print(f"      blocs structurels : {len(blocks)}")
    if blocks:
        print(f"      types : {dict(Counter(getattr(b, 'type', '?') for b in blocks))}")
        titles = [b for b in blocks if getattr(b, "type", None) == "title"]
        print(f"      titres détectés : {len(titles)}  <-- candidats de découpe")
        for b in titles[:8]:
            print(f"        · {b.content[:80]}")
    else:
        print("      ATTENTION : aucun bloc renvoyé (include_blocks sans effet ?)")
    with_header = sum(1 for p in pages if getattr(p, "header", None))
    with_conf = sum(1 for p in pages if getattr(p, "confidence_scores", None))
    print(f"      pages avec en-tête extrait : {with_header}/{len(pages)}")
    print(f"      pages avec score de confiance : {with_conf}/{len(pages)}")

    print(f"\n[3/4] Segmentation avec {MODEL_SEGMENTATION} (json_schema strict)")
    seg = client.chat.complete(
        model=MODEL_SEGMENTATION,
        temperature=0.0,
        random_seed=RANDOM_SEED,
        messages=[
            {"role": "system", "content": list_prompt},
            {"role": "user", "content": clean_document(ocr)},
        ],
        response_format=json_schema_format("rex_liste_projets", list_schema),
    )
    projects = json.loads(seg.choices[0].message.content).get("Liste", [])
    print(f"      modèle servi : {seg.model}")
    print(f"      {len(projects)} projet(s) :")
    for p in projects:
        print(f"        · p.{p['PageDebut']}-{p['PageFin']}  {p['Titre'][:60]}")
    if not projects:
        sys.exit("      Aucun projet : segmentation à revoir avant d'aller plus loin.")

    # Le 1er segment est souvent l'introduction du recueil : passer un numéro en
    # argument pour extraire une vraie fiche (ex. `smoke_test.py 6`).
    index = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    index = max(1, min(index, len(projects)))
    first = projects[index - 1]
    print(
        f"\n[4/4] Extraction de la fiche {index}/{len(projects)} "
        f"(p.{first['PageDebut']}-{first['PageFin']}) avec {MODEL_EXTRACTION} (json_schema strict)"
    )
    ext = client.chat.complete(
        model=MODEL_EXTRACTION,
        temperature=0.0,
        random_seed=RANDOM_SEED,
        messages=[
            {"role": "system", "content": rex_prompt},
            {
                "role": "user",
                "content": clean_pages(ocr, first["PageDebut"], first["PageFin"]),
            },
        ],
        response_format=json_schema_format("rex_fiche_projet", rex_schema),
    )
    data = json.loads(ext.choices[0].message.content)
    print(f"      modèle servi : {ext.model}")
    print(f"      sections renvoyées : {sorted(data.keys())}")

    print("\n      -- conformité au schéma --")
    try:
        import jsonschema

        errors = sorted(
            jsonschema.Draft7Validator(rex_schema).iter_errors(data),
            key=lambda e: list(e.path),
        )
        if errors:
            print(f"      {len(errors)} violation(s) :")
            for e in errors[:10]:
                print(f"        ! {'.'.join(str(x) for x in e.path)}: {e.message[:110]}")
        else:
            print("      aucune violation — le mode strict tient ses promesses")
    except ImportError:
        print("      (jsonschema non installé, validation ignorée)")

    print("\n      -- champs signalés par le client --")
    enjeux = data.get("Enjeux", {})
    print(f"      date_debut = {enjeux.get('date_debut')!r}   (attendu : AAAA — tâche 5)")
    print(f"      date_fin   = {enjeux.get('date_fin')!r}")
    print(f"      ramsar     = {data.get('Typologie', {}).get('type_milieu_ramsar')!r}")
    print(f"      valorisat. = {data.get('Valorisation', {}).get('type_valorisation')!r}")
    print(f"      contexte   = {data.get('Contexte', {}).get('contexte')!r}")

    usage = getattr(ext, "usage", None)
    if usage:
        print(f"\n      tokens extraction : {usage}")
    print("\nSmoke test terminé.")


if __name__ == "__main__":
    main()
