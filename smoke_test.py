#!/usr/bin/env python
"""
Vérification en direct de la chaîne Mistral.

Consomme volontairement peu de crédits sur l'extrait de 18 pages :
1 OCR + 1 segmentation + 2 extractions (deux fiches, pour MESURER le cache de
prompt : la première l'amorce, la seconde doit le toucher).

La clé est lue comme par l'application : .env en local, st.secrets en déploiement.

Usage :
    .venv/bin/python smoke_test.py                 # chaîne complète en direct
    .venv/bin/python smoke_test.py 6               # démarrer à la fiche 6
    .venv/bin/python smoke_test.py --fixture       # hors ligne, sans clé API
    .venv/bin/python smoke_test.py --batch         # soumet un lot puis l'annule
"""
import json
import sys
from collections import Counter
from pathlib import Path

from mistralai.client import Mistral

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402
from app import (  # noqa: E402
    MODEL_EXTRACTION,
    MODEL_OCR,
    MODEL_SEGMENTATION,
    clean_document,
    clean_pages,
    get_api_key,
    json_schema_format,
    load_prompt,
    load_schema,
)

PDF = Path("IFD_FICJOINT_0020373-1-18.pdf")
FIXTURE = Path("tests/fixtures/ocr-18p.json")


def api_key():
    key = get_api_key()
    if not key:
        sys.exit("MISTRAL_API_KEY absent (.env ou variable d'environnement)")
    return key


def _charger_contexte():
    rex_schema = load_schema("REX.schema.json")
    list_schema = load_schema("REXlist.schema.json")
    return {
        "rex_schema": rex_schema,
        "list_schema": list_schema,
        "rex_prompt": load_prompt("REXPrompt.md", rex_schema),
        "list_prompt": load_prompt("listPrompt.md", list_schema),
    }


def _resume_blocs(pages):
    blocs = [b for p in pages for b in (p.blocks or [])]
    print(f"      blocs structurels : {len(blocs)}")
    if blocs:
        print(f"      types : {dict(Counter(getattr(b, 'type', '?') for b in blocs))}")
        titres = [b for b in blocs if getattr(b, "type", None) == "title"]
        print(f"      titres détectés : {len(titres)}  <-- candidats de découpe")
        for b in titres[:8]:
            print(f"        · {b.content[:80]}")
    else:
        print("      ATTENTION : aucun bloc renvoyé (include_blocks sans effet ?)")
    avec_entete = sum(1 for p in pages if getattr(p, "header", None))
    avec_conf = sum(1 for p in pages if getattr(p, "confidence_scores", None))
    print(f"      pages avec en-tête extrait : {avec_entete}/{len(pages)}")
    print(f"      pages avec score de confiance : {avec_conf}/{len(pages)}")


def _verifier_conformite(data, rex_schema):
    print("\n      -- conformité au schéma --")
    try:
        import jsonschema
    except ImportError:
        print("      (jsonschema non installé, validation ignorée)")
        return
    erreurs = sorted(
        jsonschema.Draft7Validator(rex_schema).iter_errors(
            {k: v for k, v in data.items() if not k.startswith("_")}
        ),
        key=lambda e: list(e.path),
    )
    if erreurs:
        print(f"      {len(erreurs)} violation(s) :")
        for e in erreurs[:10]:
            print(f"        ! {'.'.join(str(x) for x in e.path)}: {e.message[:110]}")
    else:
        print("      aucune violation — le mode strict tient ses promesses")


def _champs_client(data):
    print("\n      -- champs signalés par le client --")
    enjeux = data.get("Enjeux", {})
    print(f"      date_debut = {enjeux.get('date_debut')!r}   (attendu : AAAA — tâche 5)")
    print(f"      date_fin   = {enjeux.get('date_fin')!r}")
    print(f"      ramsar     = {data.get('Typologie', {}).get('type_milieu_ramsar')!r}")
    print(f"      valorisat. = {data.get('Valorisation', {}).get('type_valorisation')!r}")
    print(f"      contexte   = {data.get('Contexte', {}).get('contexte')!r}")


def mode_fixture():
    """
    Chemin hors ligne : rejoue une charge OCR figée, sans clé ni appel API.

    Vérifie ce qui n'a pas besoin de l'API — l'idempotence de la charge mise en
    cache, la validation des segments, et le fait qu'un objet OCR vivant et un
    dict rechargé donnent exactement le même découpage.
    """
    if not FIXTURE.exists():
        sys.exit(
            f"Fixture absente : {FIXTURE}\n"
            "Créez-la avec une exécution en direct : "
            ".venv/bin/python smoke_test.py --figer-fixture"
        )
    charge = json.loads(FIXTURE.read_text(encoding="utf-8"))
    print(f"\n[1/3] Charge OCR figée : {FIXTURE} "
          f"({FIXTURE.stat().st_size / 1024:.0f} Ko, "
          f"{pipeline.nombre_de_pages(charge)} pages)")

    print("\n[2/3] Idempotence de la charge mise en cache")
    # Contrat réel du cache : le JSON stocké est relu tel quel, jamais
    # re-sérialisé par le SDK. On vérifie donc que la relecture est stable.
    aller = json.dumps(charge, sort_keys=True)
    retour = json.dumps(json.loads(aller), sort_keys=True)
    assert aller == retour, "la charge OCR n'est pas stable en aller-retour JSON"
    print("      aller-retour JSON stable : OK")
    from mistralai.client.models import OCRResponse
    vivant = OCRResponse.model_validate(charge)
    # Canari du piège OptionalNullable -> Unset() silencieux : on compare des
    # COMPTES, pas des identités, parce que la validation est permissive.
    blocs_dict = sum(len(p.get("blocks") or []) for p in charge["pages"])
    blocs_vivants = sum(len(p.blocks or []) for p in vivant.pages)
    conf_dict = sum(1 for p in charge["pages"] if p.get("confidence_scores"))
    conf_vivants = sum(1 for p in vivant.pages if getattr(p, "confidence_scores", None))
    assert blocs_dict == blocs_vivants, f"blocs perdus : {blocs_dict} -> {blocs_vivants}"
    assert conf_dict == conf_vivants, f"confiance perdue : {conf_dict} -> {conf_vivants}"
    print(f"      blocs conservés : {blocs_vivants} · "
          f"pages avec confiance : {conf_vivants}")
    assert clean_document(vivant) == clean_document(charge), \
        "objet OCR vivant et dict rechargé doivent donner le même document"
    assert clean_pages(vivant, 3, 5) == clean_pages(charge, 3, 5)
    print("      objet vivant et dict rechargé produisent le même découpage : OK")

    print("\n[3/3] Validation des segments")
    nb_pages = pipeline.nombre_de_pages(charge)
    cas = [
        ({"Titre": "normale", "PageDebut": 3, "PageFin": 5}, True),
        ({"Titre": "page zéro", "PageDebut": 0, "PageFin": 4}, False),
        ({"Titre": "hors document", "PageDebut": nb_pages + 5, "PageFin": nb_pages + 9}, False),
        ({"Titre": "inversée", "PageDebut": 7, "PageFin": 2}, False),
    ]
    for segment, valide in cas:
        verdict = pipeline.valider_segment(segment, nb_pages)
        assert isinstance(verdict, tuple) is valide, (segment, verdict)
        print(f"      {segment['Titre']:<15} -> {verdict}")
    travaux, echecs, _ = pipeline.preparer_segments([c[0] for c in cas], charge)
    print(f"      preparer_segments : {len(travaux)} travail(aux), "
          f"{len(echecs)} échec(s) nommé(s)")
    print("\nSmoke test hors ligne terminé — aucun appel API.")


def mode_batch(client, ctx, segments, charge_ocr):
    """
    Répétition du mode économique sans attendre 24 h : on soumet puis on annule.

    Valide ce qui casse en pratique — le purpose du fichier, la forme du JSONL,
    l'acceptation du travail — sans payer l'extraction ni patienter.
    """
    travaux, _, _ = pipeline.preparer_segments(segments, charge_ocr)
    travaux = travaux[:2]
    lignes = []
    for travail in travaux:
        requete = pipeline.construire_requete_chat(
            ctx["rex_prompt"], travail["contenu"], modele=MODEL_EXTRACTION,
            response_format=json_schema_format("rex_fiche_projet", ctx["rex_schema"]),
            prompt_cache_key=pipeline.cle_cache_prompt(
                "extraction", ctx["rex_prompt"], MODEL_EXTRACTION),
        )
        corps = {k: v for k, v in requete.items() if k != "model"}
        lignes.append(json.dumps(
            {"custom_id": f"seg-{travail['index']:03d}", "body": corps},
            ensure_ascii=False))
    jsonl = "\n".join(lignes).encode("utf-8")
    print(f"\n[lot] JSONL de {len(lignes)} ligne(s), {len(jsonl) / 1024:.0f} Ko")

    fichier = client.files.upload(
        file={"file_name": "smoke-test.jsonl", "content": jsonl}, purpose="batch")
    travail_lot = client.batch.jobs.create(
        endpoint="/v1/chat/completions", model=MODEL_EXTRACTION,
        input_files=[fichier.id], timeout_hours=24,
        metadata={"application": "rex-mh", "smoke_test": "1"})
    print(f"      travail créé : {travail_lot.id} · statut {travail_lot.status}")
    annule = client.batch.jobs.cancel(job_id=travail_lot.id)
    print(f"      annulé : statut {annule.status}")
    try:
        client.files.delete(file_id=fichier.id)
        print("      JSONL supprimé")
    except Exception as exc:
        print(f"      (suppression du JSONL impossible : {exc})")


def main():
    arguments = sys.argv[1:]
    if "--fixture" in arguments:
        return mode_fixture()

    figer = "--figer-fixture" in arguments
    faire_lot = "--batch" in arguments
    index = next((int(a) for a in arguments if a.isdigit()), 1)

    if not PDF.exists():
        sys.exit(f"PDF de test introuvable : {PDF}")

    client = Mistral(api_key=api_key())
    ctx = _charger_contexte()
    if not all(ctx.values()):
        sys.exit("Prompts ou schémas illisibles.")

    print(f"\n[1/4] Upload de {PDF.name} ({PDF.stat().st_size / 1024:.0f} Ko)")
    televerse = client.files.upload(
        file={"file_name": PDF.name, "content": PDF.read_bytes()}, purpose="ocr")
    signed = client.files.get_signed_url(file_id=televerse.id)
    print(f"      OK, file_id={televerse.id}")

    print(f"\n[2/4] OCR avec {MODEL_OCR}")
    ocr = client.ocr.process(
        model=MODEL_OCR,
        document={"type": "document_url", "document_url": signed.url},
        **pipeline.OCR_PARAMS,
    )
    print(f"      {len(ocr.pages)} pages")
    _resume_blocs(ocr.pages)
    confiance = pipeline.confiance_moyenne(ocr)
    if confiance is not None:
        print(f"      confiance moyenne : {confiance:.3f}")
    try:
        client.files.delete(file_id=televerse.id)
    except Exception as exc:
        print(f"      (suppression du PDF distant impossible : {exc})")

    if figer:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(ocr.model_dump_json(), encoding="utf-8")
        print(f"      charge OCR figée dans {FIXTURE} "
              f"({FIXTURE.stat().st_size / 1024:.0f} Ko)")

    print(f"\n[3/4] Segmentation avec {MODEL_SEGMENTATION} (json_schema strict)")
    seg = client.chat.complete(
        **pipeline.construire_requete_chat(
            ctx["list_prompt"], clean_document(ocr), modele=MODEL_SEGMENTATION,
            response_format=json_schema_format("rex_liste_projets", ctx["list_schema"]),
            prompt_cache_key=pipeline.cle_cache_prompt(
                "segmentation", ctx["list_prompt"], MODEL_SEGMENTATION),
        )
    )
    segments = json.loads(seg.choices[0].message.content).get("Liste", [])
    print(f"      modèle servi : {seg.model}")
    print(f"      {len(segments)} projet(s) :")
    for s in segments:
        print(f"        · p.{s['PageDebut']}-{s['PageFin']}  {s['Titre'][:60]}")
    if not segments:
        sys.exit("      Aucun projet : segmentation à revoir avant d'aller plus loin.")

    travaux, echecs, avertissements = pipeline.preparer_segments(segments, ocr)
    print(f"      segments retenus : {len(travaux)} · refusés : {len(echecs)}")
    for echec in echecs:
        print(f"        ! {echec['titre'][:40]} — {echec['error']}")
    for avertissement in avertissements:
        print(f"        ~ {avertissement}")

    if faire_lot:
        mode_batch(client, ctx, segments, ocr)
        print("\nSmoke test terminé (mode lot).")
        return

    # Deux fiches consécutives : la première amorce le cache de prompt, la
    # seconde doit le toucher. C'est la seule façon de MESURER le gain — une
    # extraction unique donne toujours cached_tokens: 0.
    depart = max(1, min(index, len(travaux)))
    a_extraire = travaux[depart - 1:depart + 1]
    format_extraction = json_schema_format("rex_fiche_projet", ctx["rex_schema"])
    cle_cache = pipeline.cle_cache_prompt("extraction", ctx["rex_prompt"], MODEL_EXTRACTION)
    print(f"\n[4/4] Extraction de {len(a_extraire)} fiche(s) avec {MODEL_EXTRACTION}")
    print(f"      clé de cache de prompt : {cle_cache}")

    mesures = []
    for rang, travail in enumerate(a_extraire, start=depart):
        reponse = client.chat.complete(
            **pipeline.construire_requete_chat(
                ctx["rex_prompt"], travail["contenu"], modele=MODEL_EXTRACTION,
                response_format=format_extraction, prompt_cache_key=cle_cache,
            )
        )
        data = json.loads(reponse.choices[0].message.content)
        usage = pipeline.usage_depuis_reponse(reponse)
        mesures.append(usage)
        print(f"\n      fiche {rang}/{len(travaux)} "
              f"(p.{travail['debut']}-{travail['fin']}) — {travail['titre'][:50]}")
        print(f"      modèle servi : {reponse.model}")
        print(f"      jetons prompt {usage['prompt_tokens']} · "
              f"cache {usage['cached_tokens']} ({pipeline.taux_cache(usage):.0%}) · "
              f"génération {usage['completion_tokens']}")
        if rang == depart:
            print(f"      sections renvoyées : {sorted(k for k in data if not k.startswith('_'))}")
            _verifier_conformite(data, ctx["rex_schema"])
            _champs_client(data)

    print("\n      -- cache de prompt --")
    if len(mesures) < 2:
        print("      une seule fiche extraite : mesure du cache impossible.")
    else:
        amorce, suivante = mesures[0], mesures[1]
        print(f"      fiche d'amorçage : {amorce['cached_tokens']} jeton(s) en cache")
        print(f"      fiche suivante   : {suivante['cached_tokens']} jeton(s) en cache "
              f"({pipeline.taux_cache(suivante):.0%})")
        if suivante["cached_tokens"] == 0:
            print("      ÉCHEC : le cache ne s'engage pas. Vérifier que "
                  "prompt_cache_key est bien transmis et que le prompt système "
                  "est identique d'un appel à l'autre.")
        else:
            reste = suivante["cached_tokens"] % 64
            print(f"      blocs de 64 jetons : {suivante['cached_tokens']} "
                  f"({'multiple exact' if reste == 0 else f'reste {reste}'})")
            if pipeline.taux_cache(suivante) < 0.60:
                print("      ATTENTION : taux inférieur à la cible de 60 %.")
            else:
                print("      cible atteinte (>= 60 % des jetons de prompt en cache).")

    print("\nSmoke test terminé.")


if __name__ == "__main__":
    main()
