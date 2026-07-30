
import functools
import html
import importlib.metadata
import json
import os
import sqlite3
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from st_mui_table import st_mui_table

import pipeline
import store
# Réexportés pour que smoke_test.py et l'interface parlent le même vocabulaire
# que le pipeline. La logique, elle, vit dans pipeline.py — qui n'importe pas
# streamlit, seul garde-fou fiable contre une lecture de st.session_state depuis
# un thread de travail (voir le docstring de pipeline.py).
from pipeline import (  # noqa: F401
    MODEL_EXTRACTION,
    MODEL_OCR,
    MODEL_SEGMENTATION,
    RANDOM_SEED,
    clean_document,
    clean_pages,
    json_schema_format,
    strict_schema,
)

# Charge .env en local ; sans effet en déploiement (Streamlit Cloud utilise st.secrets).
load_dotenv()

# Clés de session portant les prompts et schémas, vidées par le bouton de
# rechargement (ils sont chargés une fois par session, pas par rerun).
CLES_PROMPTS = ("REXSchema", "REXListSchema", "REXPrompt", "listPrompt")


# Configure page
st.set_page_config(
    page_title="REX Zones Humides",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="collapsed"
)


@st.cache_data(show_spinner=False, max_entries=32)
def load_text_file(path, mtime):
    """
    Contenu texte d'un fichier du dépôt, mis en cache.

    `mtime` n'est pas utilisé dans le corps : il fait partie de la clé de cache,
    si bien qu'éditer le fichier invalide l'entrée sans redémarrer le serveur.
    Sans ce cache, styles.css (14 Ko) était relu à CHAQUE rerun.
    """
    return Path(path).read_text(encoding="utf-8")


def _cle_fichier(path):
    """(chemin, date de modification) — la clé de cache de load_text_file."""
    return str(path), os.path.getmtime(path)


def load_css(file):
    """Injecte la feuille de style. Le fichier n'est relu qu'après édition."""
    st.html(f"<style>{load_text_file(*_cle_fichier(file))}</style>")


css_path = "styles.css"
load_css(css_path)


def load_schema(schema_name):
    """Schéma JSON du dépôt, ou None (avec un message) s'il est illisible."""
    if not Path(schema_name).exists():
        st.error(f"Schéma introuvable : {schema_name}")
        return None
    try:
        return json.loads(load_text_file(*_cle_fichier(schema_name)))
    except Exception as e:
        st.error(f"Erreur au chargement du schéma {schema_name} : {e}")
        return None


def load_prompt(prompt_name, schema=None):
    """
    Prompt Markdown du dépôt, schéma substitué au placeholder {{ SCHEMA_JSON }}.

    Le schéma est du payload de prompt autant qu'un contrat de validation :
    l'éditer change ce qui est demandé au modèle.
    """
    if not Path(prompt_name).exists():
        st.error(f"Prompt introuvable : {prompt_name}")
        return None
    try:
        contenu = load_text_file(*_cle_fichier(prompt_name))
        if schema is not None:
            contenu = contenu.replace(
                "{{ SCHEMA_JSON }}", json.dumps(schema, indent=2, ensure_ascii=False)
            )
        return contenu
    except Exception as e:
        st.error(f"Erreur au chargement du prompt {prompt_name} : {e}")
        return None


def get_api_key():
    """
    Clé API Mistral, dans l'ordre :
      1. st.secrets  — utilisé par Streamlit Community Cloud (rex-mh-oieau.streamlit.app)
      2. variable d'environnement / .env  — développement local

    st.secrets lève une exception quand aucun secrets.toml n'existe, d'où le try.
    """
    try:
        key = st.secrets["MISTRAL_API_KEY"]
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("MISTRAL_API_KEY")


def get_db_path():
    """
    Chemin de la base d'historique, dans l'ordre :
      1. st.secrets["REX_DB_PATH"]  — déploiement
      2. $REX_DB_PATH / .env        — développement local
      3. data/rex.db                — défaut

    Sur Streamlit Community Cloud le disque est éphémère : la base disparaît à
    chaque redéploiement, d'où la sauvegarde/restauration de l'historique.
    """
    try:
        chemin = st.secrets["REX_DB_PATH"]
        if chemin:
            return chemin
    except Exception:
        pass
    return os.environ.get("REX_DB_PATH") or store.DEFAULT_DB_PATH


@st.cache_resource(show_spinner=False)
def obtenir_client(_empreinte_cle):
    """
    Un seul client Mistral pour tout le processus serveur.

    `st.cache_resource` garde une référence forte, donc le finaliseur posé par le
    SDK ne se déclenchera pas en pleine exécution. L'objet est partagé entre
    toutes les sessions navigateur : c'est voulu et sans danger, un pool httpx
    synchrone est thread-safe et sans état de session. NE RIEN mettre de
    spécifique à une session à côté.

    L'argument n'est qu'une empreinte de la clé (jamais la clé) : il sert
    uniquement à invalider le cache si la clé change en cours de vie du serveur.
    """
    cle = get_api_key()
    if not cle:
        raise ValueError(
            "MISTRAL_API_KEY introuvable — renseignez .env en local "
            "ou les secrets de l'application en déploiement."
        )
    return pipeline.construire_client(cle)


def client_mistral():
    """Client prêt à l'emploi, ou None si la clé manque (message affiché)."""
    cle = get_api_key()
    if not cle:
        st.error(
            "MISTRAL_API_KEY introuvable — renseignez .env en local "
            "ou les secrets de l'application en déploiement."
        )
        return None
    return obtenir_client(pipeline.empreinte(cle)[:16])


@st.cache_resource(show_spinner=False)
def _cles_echauffees():
    """
    Clés de cache de prompt déjà écrites par ce processus serveur.

    Heuristique assumée : la durée de vie côté serveur n'est pas observable. Se
    tromper ne coûte qu'un hit manqué, jamais un résultat faux — d'où l'ensemble
    non verrouillé (ajouter un élément à un set est atomique sous GIL).
    """
    return set()

def _contexte_extraction():
    """
    Prompts, schémas et empreintes lus UNE FOIS sur le thread du script.

    C'est le point de passage obligé avant toute mise en parallèle. Un thread de
    travail sans ScriptRunContext qui lit `st.session_state` ne reçoit pas une
    erreur claire : Streamlit lui sert un état factice global au processus, si
    bien que les N fiches échouent identiquement sur un AttributeError. Tout ce
    dont le pipeline a besoin est donc extrait ici, en valeurs simples.
    """
    prompt_extraction = st.session_state.REXPrompt
    prompt_segmentation = st.session_state.listPrompt
    schema_rex = st.session_state.REXSchema
    schema_liste = st.session_state.REXListSchema
    return {
        "prompt_extraction": prompt_extraction,
        "prompt_segmentation": prompt_segmentation,
        "format_extraction": json_schema_format("rex_fiche_projet", schema_rex),
        "format_segmentation": json_schema_format("rex_liste_projets", schema_liste),
        "cle_cache_extraction": pipeline.cle_cache_prompt(
            "extraction", prompt_extraction, MODEL_EXTRACTION
        ),
        "cle_cache_segmentation": pipeline.cle_cache_prompt(
            "segmentation", prompt_segmentation, MODEL_SEGMENTATION
        ),
        "hash_prompt_extraction": pipeline.empreinte(prompt_extraction),
        "hash_prompt_segmentation": pipeline.empreinte(prompt_segmentation),
        "hash_schema_rex": pipeline.empreinte(
            json.dumps(schema_rex, sort_keys=True, ensure_ascii=False)
        ),
        "hash_schema_liste": pipeline.empreinte(
            json.dumps(schema_liste, sort_keys=True, ensure_ascii=False)
        ),
    }


def _avancer(progress_callback, phase, fraction_phase, message, **extra):
    """
    Progression pondérée par phase.

    L'ancien schéma indexait l'avancement sur la SOUMISSION des appels : sous un
    pool, la barre sauterait à 100 % dès le dernier envoi et le texte nommerait
    une fiche au hasard parmi celles en vol. On compose donc les phases terminées
    plus la fraction de la phase courante.

    Les deux premiers arguments restent positionnels (float, str) : les appelants
    existants n'ont qu'à absorber les kwargs. Le clamp est nécessaire, st.progress
    lève hors de [0, 1].
    """
    if progress_callback is None:
        return
    poids = pipeline.POIDS_PHASES
    base = sum(v for k, v in poids.items() if _ORDRE_PHASES.index(k) < _ORDRE_PHASES.index(phase))
    avancement = base + poids[phase] * max(0.0, min(1.0, fraction_phase))
    progress_callback(max(0.0, min(1.0, avancement)), message, phase=phase, **extra)


_ORDRE_PHASES = ("upload", "ocr", "segmentation", "extraction")


def _ocr_du_document(client, file_content, filename, *, document_id, cle_ocr,
                     progress_callback=None):
    """
    Charge OCR du document : depuis le cache si possible, sinon appel à l'API.

    Renvoie (charge, depuis_le_cache, model_ocr). La charge est un dict (cache) ou
    une réponse OCR vivante — `pipeline._ocr_pages` accepte les deux, donc aucun
    appelant n'a à faire la différence.
    """
    payload = store.get_ocr_payload(document_id, cle_ocr=cle_ocr)
    if payload is not None:
        _avancer(progress_callback, "ocr", 1.0, "OCR déjà en cache pour ce document")
        charge = json.loads(payload)
        meta = store.get_ocr_meta(document_id) or {}
        return charge, True, meta.get("model") or MODEL_OCR

    _avancer(progress_callback, "upload", 0.5, "Envoi du fichier à Mistral…")
    televerse = client.files.upload(
        file={"file_name": filename, "content": file_content},
        purpose="ocr",
        timeout_ms=pipeline.TIMEOUT_UPLOAD_MS,
        retries=pipeline.RETRY_UPLOAD,
    )
    try:
        signed = client.files.get_signed_url(file_id=televerse.id)
        _avancer(progress_callback, "ocr", 0.1, "Reconnaissance de texte (OCR)…")
        ocr = client.ocr.process(
            model=MODEL_OCR,
            document={"type": "document_url", "document_url": signed.url},
            timeout_ms=pipeline.TIMEOUT_OCR_MS,
            retries=pipeline.RETRY_OCR,
            **pipeline.OCR_PARAMS,
        )
    finally:
        # Le PDF téléversé n'était jamais supprimé : chaque traitement laissait un
        # fichier dans l'espace de travail Mistral. Best-effort, jamais fatal.
        try:
            client.files.delete(file_id=televerse.id)
        except Exception as exc:
            print(f"Avertissement : suppression du fichier téléversé impossible ({exc})")

    # Prise UNE SEULE FOIS sur la réponse vivante, puis jamais re-sérialisée :
    # le round-trip Pydantic n'est pas idempotent sur les blocs de type inconnu.
    store.save_ocr_payload(
        document_id,
        ocr.model_dump_json(),
        cle_ocr=cle_ocr,
        model=pipeline._resolved_model(ocr, MODEL_OCR),
        pages_processed=getattr(getattr(ocr, "usage_info", None), "pages_processed", None),
        avg_confidence=pipeline.confiance_moyenne(ocr),
        sdk_version=_version_mistralai(),
    )
    store.set_document_pages(document_id, pipeline.nombre_de_pages(ocr))
    _avancer(progress_callback, "ocr", 1.0,
             f"OCR terminé — {pipeline.nombre_de_pages(ocr)} page(s)")
    return ocr, False, pipeline._resolved_model(ocr, MODEL_OCR)


def _version_mistralai():
    try:
        return importlib.metadata.version("mistralai")
    except Exception:
        return None


def _segmenter(client, charge_ocr, ctx, *, progress_callback=None):
    """Découpe le recueil en fiches. Renvoie (segments, model, usage)."""
    _avancer(progress_callback, "segmentation", 0.2, "Découpage du recueil en fiches…")
    requete = pipeline.construire_requete_chat(
        ctx["prompt_segmentation"],
        clean_document(charge_ocr),
        modele=MODEL_SEGMENTATION,
        response_format=ctx["format_segmentation"],
        prompt_cache_key=ctx["cle_cache_segmentation"],
    )
    reponse = client.chat.complete(
        **requete,
        timeout_ms=pipeline.TIMEOUT_SEGMENTATION_MS,
        retries=pipeline.RETRY_SEGMENTATION,
    )
    contenu = reponse.choices[0].message.content
    try:
        segments = json.loads(contenu).get("Liste", [])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Liste de projets illisible : {exc}") from exc
    _avancer(progress_callback, "segmentation", 1.0,
             f"{len(segments)} fiche(s) repérée(s)")
    return (segments,
            pipeline._resolved_model(reponse, MODEL_SEGMENTATION),
            pipeline.usage_depuis_reponse(reponse))


def _resultat_vide(**kw):
    """Forme de retour du pipeline, unique pour les deux modes."""
    resultat = {
        "document_id": None,
        "run_id": None,
        "mode": "rapide",
        "statut": "termine",
        "projects": [],
        "failures": [],
        "usage": pipeline.usage_neuf(),
        "ocr_cache_hit": False,
        "job_id": None,
        "avertissements": [],
        "filename": None,
    }
    resultat.update(kw)
    return resultat


def parse_pdf_document(file_content, filename, progress_callback=None, mode="rapide"):
    """
    Traite un PDF de bout en bout : OCR, découpage, extraction de chaque fiche.

    Args:
        file_content: contenu binaire du PDF
        filename: nom du fichier (informatif ; l'identité est le hash du contenu)
        progress_callback: appelable (avancement: float, message: str, **détails)
        mode: « rapide » (parallèle, résultat immédiat) ou « economique »
              (API par lot, -50 %, résultat à récolter plus tard)

    Returns:
        dict: voir _resultat_vide — projects, failures, usage, run_id…
        Les échecs par fiche ne sont plus perdus dans un print() : ils sont
        nommés, catégorisés et réessayables.
    """
    client = client_mistral()
    if client is None:
        return _resultat_vide(statut="echec")

    ctx = _contexte_extraction()
    sha = pipeline.sha256_fichier(file_content)
    cle_ocr = pipeline.cle_cache_ocr(file_content)
    document_id = store.get_or_create_document(sha, filename, size_bytes=len(file_content))

    try:
        run_id, _ = store.start_run(
            document_id,
            mode=mode,
            prompt_extraction_sha256=ctx["hash_prompt_extraction"],
            prompt_segmentation_sha256=ctx["hash_prompt_segmentation"],
            schema_rex_sha256=ctx["hash_schema_rex"],
            schema_list_sha256=ctx["hash_schema_liste"],
        )
    except sqlite3.IntegrityError:
        # Index partiel « un seul run en cours par document » : un autre onglet
        # (ou un double clic) traite déjà ce PDF. Ne pas repayer.
        return _resultat_vide(
            document_id=document_id, mode=mode, statut="echec",
            avertissements=["Ce document est déjà en cours de traitement."],
        )

    usage = pipeline.usage_neuf()
    resultat = _resultat_vide(document_id=document_id, run_id=run_id, mode=mode,
                              usage=usage, filename=filename)
    try:
        charge_ocr, depuis_cache, model_ocr = _ocr_du_document(
            client, file_content, filename, document_id=document_id,
            cle_ocr=cle_ocr, progress_callback=progress_callback,
        )
        resultat["ocr_cache_hit"] = depuis_cache
        usage["ocr"] = {
            "pages_traitees": pipeline.nombre_de_pages(charge_ocr),
            "octets": len(file_content),
        }

        segments, model_segmentation, usage_seg = _segmenter(
            client, charge_ocr, ctx, progress_callback=progress_callback)
        pipeline.usage_cumuler(usage["segmentation"], usage_seg)
        pipeline.usage_cumuler(usage["total"], usage_seg)
        store.set_run_segmentation(run_id, json.dumps({"Liste": segments},
                                                     ensure_ascii=False),
                                   model_segmentation=model_segmentation)
        store.set_run_models(run_id, model_ocr=model_ocr,
                             model_segmentation=model_segmentation)
        store.add_run_usage(run_id, **_jetons(usage_seg))

        if not segments:
            store.finish_run(run_id, status="echec",
                             error="aucune fiche repérée dans le document")
            resultat["statut"] = "echec"
            resultat["avertissements"].append(
                "Aucune fiche n'a été repérée dans ce document.")
            return resultat

        travaux, echecs, avertissements = pipeline.preparer_segments(segments, charge_ocr)
        resultat["failures"].extend(echecs)
        resultat["avertissements"].extend(avertissements)
        modeles_traces = {"ocr": model_ocr, "segmentation": model_segmentation}
        for echec in echecs:
            _persister_echec(run_id, document_id, echec)

        if mode == "economique":
            return _soumettre_par_lot(
                client, resultat, travaux=travaux, ctx=ctx,
                document_id=document_id, run_id=run_id, filename=filename,
                progress_callback=progress_callback,
            )

        fiches, echecs_extraction, usage_ext = _extraire_en_parallele(
            client, travaux, ctx=ctx, modeles_traces=modeles_traces,
            document_id=document_id, run_id=run_id,
            progress_callback=progress_callback,
        )
        resultat["projects"] = fiches
        resultat["failures"].extend(echecs_extraction)
        resultat["failures"].sort(key=lambda e: e["index"])
        pipeline.usage_cumuler(usage["extraction"], usage_ext)
        pipeline.usage_cumuler(usage["total"], usage_ext)

        resultat["statut"] = _statut_du_run(resultat)
        store.finish_run(run_id, status=resultat["statut"])
        _avancer(progress_callback, "extraction", 1.0,
                 f"Traitement terminé — {len(fiches)} fiche(s) extraite(s)"
                 + (f", {len(resultat['failures'])} en échec"
                    if resultat["failures"] else ""))
        return resultat

    except Exception as exc:
        store.finish_run(run_id, status="echec", error=f"{type(exc).__name__}: {exc}")
        resultat["statut"] = "echec"
        if progress_callback:
            progress_callback(0.0, f"Erreur : {exc}", phase="extraction")
        raise


def _statut_du_run(resultat):
    if not resultat["projects"]:
        return "echec"
    return "partiel" if resultat["failures"] else "termine"


def _jetons(usage):
    """Sous-ensemble de l'usage que store.add_run_usage attend."""
    return {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


def _persister_echec(run_id, document_id, echec):
    debut, fin = echec.get("pages", (None, None))
    store.upsert_fiche(
        run_id, document_id, echec["index"], status="echec",
        titre=echec.get("titre"), page_debut=debut, page_fin=fin,
        error=echec.get("error"), categorie=echec.get("categorie"),
    )


def _extraire_en_parallele(client, travaux, *, ctx, modeles_traces, document_id,
                           run_id, progress_callback=None):
    """
    Mode « rapide » : échauffement du cache puis fan-out.

    La persistance et la progression se font ici, sur le thread du script, dans
    le rappel d'achèvement — aucun worker n'écrit en base, donc aucun verrou. Un
    plantage en cours de route conserve les fiches déjà faites.
    """
    total = len(travaux)
    etat = {"faits": 0, "echecs": 0}
    cle_cache = ctx["cle_cache_extraction"]

    def on_resultat(index, fiche, echec, usage_fiche):
        etat["faits"] += 1
        if fiche is not None:
            store.upsert_fiche(
                run_id, document_id, index, status="ok",
                titre=fiche.get("_project_title"),
                page_debut=fiche.get("_page_debut"), page_fin=fiche.get("_page_fin"),
                data=fiche, model_extraction=fiche.get("_model_extraction"),
                prompt_hash=fiche.get("_prompt_hash"), usage=usage_fiche,
            )
        if echec is not None:
            etat["echecs"] += 1
            _persister_echec(run_id, document_id, echec)
        store.add_run_usage(run_id, **_jetons(usage_fiche))
        _avancer(progress_callback, "extraction", etat["faits"] / max(total, 1),
                 f"Extraction {etat['faits']}/{total} fiche(s)"
                 + (f" — {etat['echecs']} en échec" if etat["echecs"] else ""),
                 faits=etat["faits"], total=total, echecs=etat["echecs"])

    _avancer(progress_callback, "extraction", 0.0,
             f"Extraction de {total} fiche(s) — amorçage du cache…",
             faits=0, total=total)
    fiches, echecs, usage_total = pipeline.extraire_fiches(
        client, travaux,
        prompt_systeme=ctx["prompt_extraction"],
        response_format=ctx["format_extraction"],
        prompt_cache_key=cle_cache,
        modeles_traces=modeles_traces,
        deja_echauffe=cle_cache in _cles_echauffees(),
        on_resultat=on_resultat,
    )
    _cles_echauffees().add(cle_cache)
    if fiches:
        store.set_run_models(run_id, model_extraction=fiches[0]["_model_extraction"])
    return fiches, echecs, usage_total


def relancer_fiches(document_id, run_id, indices, progress_callback=None):
    """
    Ré-extrait une sélection de fiches SANS retoucher à l'OCR : ni téléversement,
    ni ocr.process, ni appel de segmentation. La charge vient du cache et la clé
    de cache de prompt est la même, donc un réessai coûte ce que coûte une fiche
    2+ d'un run normal.

    Garde-fou : si le prompt a changé depuis le run initial, la clé de cache
    change PAR CONSTRUCTION et la fiche relancée sort d'un prompt différent de
    ses sœurs. `_prompt_hash` est stocké par fiche pour que ce mélange soit
    visible au lieu d'être silencieux.
    """
    client = client_mistral()
    if client is None:
        return None
    run = store.get_run(run_id)
    payload = store.get_ocr_payload(document_id)
    if run is None or payload is None:
        st.error("Charge OCR absente du cache : relancez le document complet.")
        return None

    charge_ocr = json.loads(payload)
    segments = json.loads(run["segmentation_json"] or '{"Liste": []}').get("Liste", [])
    travaux, _, _ = pipeline.preparer_segments(segments, charge_ocr)
    retenus = [t for t in travaux if t["index"] in set(indices)]
    if not retenus:
        st.warning("Aucune fiche relançable dans cette sélection.")
        return None

    ctx = _contexte_extraction()
    modeles_traces = {"ocr": run["model_ocr"], "segmentation": run["model_segmentation"]}
    fiches, echecs, _ = _extraire_en_parallele(
        client, retenus, ctx=ctx, modeles_traces=modeles_traces,
        document_id=document_id, run_id=run_id, progress_callback=progress_callback,
    )
    store.finish_run(
        run_id,
        status="termine" if not store.load_failures(run_id) else "partiel",
    )
    return {"relancees": len(fiches), "encore_en_echec": len(echecs)}


# --- Mode « économique » : API par lot ---------------------------------------


def _soumettre_par_lot(client, resultat, *, travaux, ctx, document_id, run_id,
                       filename, progress_callback=None):
    """
    Soumet l'extraction en un travail par lot (-50 %, latence asynchrone).

    Ne bloque pas : le suivi se fait à un run Streamlit ultérieur, via le panneau
    des travaux en attente. Un run de script détient le thread de la session et
    meurt avec le websocket — aucune boucle d'attente ici.
    """
    lignes, correspondance = [], {}
    for travail in travaux:
        requete = pipeline.construire_requete_chat(
            ctx["prompt_extraction"], travail["contenu"],
            modele=MODEL_EXTRACTION,
            response_format=ctx["format_extraction"],
            prompt_cache_key=ctx["cle_cache_extraction"],
        )
        # `model` est fixé une fois au niveau du travail : une seule source de
        # vérité, et un JSONL deux fois plus léger.
        corps = {k: v for k, v in requete.items() if k != "model"}
        custom_id = f"seg-{travail['index']:03d}"
        correspondance[custom_id] = travail["index"]
        lignes.append(json.dumps({"custom_id": custom_id, "body": corps},
                                 ensure_ascii=False))

    _avancer(progress_callback, "extraction", 0.2,
             f"Envoi de {len(lignes)} requête(s) en traitement par lot…")
    jsonl = "\n".join(lignes).encode("utf-8")
    fichier = client.files.upload(
        file={"file_name": f"rex-run-{run_id}.jsonl", "content": jsonl},
        purpose="batch",          # et non "ocr" : ce n'est pas un document
        timeout_ms=pipeline.TIMEOUT_UPLOAD_MS,
        retries=pipeline.RETRY_UPLOAD,
    )
    travail_lot = client.batch.jobs.create(
        endpoint="/v1/chat/completions",
        model=MODEL_EXTRACTION,
        input_files=[fichier.id],
        timeout_hours=24,
        # Rend le travail auto-descriptif : `jobs.list()` suffit à retrouver à
        # quel document il appartient, même si la base est perdue.
        metadata={"application": "rex-mh", "run_id": str(run_id),
                  "document_id": str(document_id), "fichier": filename[:100]},
    )
    # Persister AVANT de rendre la main : c'est l'ancre de reprise.
    store.record_batch_job(
        travail_lot.id, run_id=run_id, document_id=document_id,
        endpoint="/v1/chat/completions", kind="extraction",
        status=str(getattr(travail_lot, "status", "QUEUED")),
        input_file_id=fichier.id, fiche_seq_map=correspondance,
    )
    for travail in travaux:
        store.upsert_fiche(run_id, document_id, travail["index"],
                           status="en_attente", titre=travail["titre"],
                           page_debut=travail["debut"], page_fin=travail["fin"])

    resultat["job_id"] = travail_lot.id
    resultat["statut"] = "en_attente"
    _avancer(progress_callback, "extraction", 1.0,
             f"Travail par lot soumis ({len(lignes)} fiche(s)) — résultats à récolter")
    return resultat


def _lire_ligne_batch(ligne):
    """(custom_id, contenu du message, message d'erreur) — parsing défensif."""
    custom_id = ligne.get("custom_id")
    reponse = ligne.get("response") or {}
    code = reponse.get("status_code")
    corps = reponse.get("body") or {}
    erreur = ligne.get("error")
    if erreur or (code is not None and code >= 400):
        return custom_id, None, f"lot {code} : {erreur or corps}"
    try:
        return custom_id, corps["choices"][0]["message"]["content"], None
    except (KeyError, IndexError, TypeError) as exc:
        return custom_id, None, f"réponse de lot de forme inattendue : {exc}"


def actualiser_travail_par_lot(job_id):
    """
    Sonde un travail par lot et récolte ses résultats s'il est terminé.

    Renvoie un dict de compte-rendu, ou None si le client est indisponible.
    """
    client = client_mistral()
    if client is None:
        return None
    enregistre = store.get_batch_job(job_id)
    if enregistre is None:
        st.error(f"Travail {job_id} inconnu de l'historique.")
        return None

    travail = client.batch.jobs.get(job_id=job_id, inline=True)
    statut = str(getattr(travail, "status", "")) or "UNKNOWN"
    store.refresh_batch_job(
        job_id, status=statut,
        output_file_id=getattr(travail, "output_file", None),
        error_file_id=getattr(travail, "error_file", None),
        total_requests=getattr(travail, "total_requests", None),
        succeeded_requests=getattr(travail, "succeeded_requests", None),
        failed_requests=getattr(travail, "failed_requests", None),
    )
    if statut not in store.STATUTS_BATCH_TERMINAUX:
        return {"statut": statut, "recolte": False}
    if statut == "CANCELLED":
        store.finish_run(enregistre["run_id"], status="echec", error="travail annulé")
        return {"statut": statut, "recolte": False}

    return _recolter_travail_par_lot(client, travail, enregistre)


def _recolter_travail_par_lot(client, travail, enregistre):
    """Rattache chaque ligne de sortie à sa fiche, et nomme les manquantes."""
    run_id, document_id = enregistre["run_id"], enregistre["document_id"]
    correspondance = json.loads(enregistre["fiche_seq_map_json"] or "{}")
    run = store.get_run(run_id)

    lignes = []
    sorties = getattr(travail, "outputs", None)
    if sorties:
        lignes = list(sorties)
    elif getattr(travail, "output_file", None):
        reponse = client.files.download(file_id=travail.output_file)
        lignes = [json.loads(l) for l in reponse.text.splitlines() if l.strip()]
    if getattr(travail, "error_file", None):
        try:
            brut = client.files.download(file_id=travail.error_file)
            lignes += [json.loads(l) for l in brut.text.splitlines() if l.strip()]
        except Exception as exc:
            print(f"Avertissement : fichier d'erreurs du lot illisible ({exc})")

    fiches_par_seq = {
        f["seq"]: f for f in store.list_fiches(run_id)
    }
    nb_ok = nb_echec = 0
    vus = set()
    for ligne in lignes:
        custom_id, contenu, erreur = _lire_ligne_batch(ligne)
        seq = correspondance.get(custom_id)
        if seq is None:
            continue
        vus.add(custom_id)
        reference = fiches_par_seq.get(seq, {})
        if erreur is not None:
            nb_echec += 1
            store.upsert_fiche(run_id, document_id, seq, status="echec",
                               titre=reference.get("titre"),
                               page_debut=reference.get("page_debut"),
                               page_fin=reference.get("page_fin"),
                               error=erreur, categorie="lot")
            continue
        try:
            fiche = json.loads(contenu)
        except json.JSONDecodeError as exc:
            nb_echec += 1
            store.upsert_fiche(run_id, document_id, seq, status="echec",
                               titre=reference.get("titre"),
                               page_debut=reference.get("page_debut"),
                               page_fin=reference.get("page_fin"),
                               error=f"JSON invalide : {exc}", categorie="json_invalide")
            continue
        nb_ok += 1
        store.upsert_fiche(
            run_id, document_id, seq, status="ok",
            titre=reference.get("titre"), page_debut=reference.get("page_debut"),
            page_fin=reference.get("page_fin"), data=fiche,
            model_extraction=(run or {}).get("model_extraction") or MODEL_EXTRACTION,
            prompt_hash=(run or {}).get("prompt_extraction_sha256", "")[:16],
        )

    # Un custom_id présent dans les segments mais ABSENT des sorties est lui-même
    # un échec : sans ce contrôle, un fichier de sortie tronqué ferait disparaître
    # des fiches en silence — exactement la classe de bug qu'on supprime.
    manquants = 0
    for custom_id, seq in correspondance.items():
        if custom_id in vus:
            continue
        manquants += 1
        reference = fiches_par_seq.get(seq, {})
        store.upsert_fiche(run_id, document_id, seq, status="echec",
                           titre=reference.get("titre"),
                           page_debut=reference.get("page_debut"),
                           page_fin=reference.get("page_fin"),
                           error="aucune ligne de résultat pour ce segment",
                           categorie="absent_lot")

    # Les erreurs de niveau travail (JSONL mal formé, modèle inconnu) ne sont pas
    # des échecs de fiche : elles décrivent le run.
    erreurs_travail = getattr(travail, "errors", None) or []
    message = "; ".join(
        f"{getattr(e, 'message', e)} (×{getattr(e, 'count', 1)})" for e in erreurs_travail
    ) or None
    store.finish_run(
        run_id,
        status="termine" if nb_ok and not (nb_echec + manquants) else
               ("partiel" if nb_ok else "echec"),
        error=message,
    )
    if enregistre.get("input_file_id"):
        try:
            client.files.delete(file_id=enregistre["input_file_id"])
        except Exception as exc:
            print(f"Avertissement : suppression du JSONL impossible ({exc})")

    return {"statut": str(getattr(travail, "status", "")), "recolte": True,
            "ok": nb_ok, "echecs": nb_echec, "manquants": manquants,
            "run_id": run_id, "erreurs_travail": message}


def process_uploaded_file(file, filename, mode="rapide"):
    """Traite un PDF déposé, avec barre de progression."""
    with st.container():
        barre = st.progress(0)
        texte = st.empty()

        def update_progress(progress, status, **_):
            # **_ absorbe les détails (phase, faits, total, echecs) que la
            # tâche 4 pourra exploiter sans retoucher au pipeline.
            barre.progress(max(0.0, min(1.0, progress)))
            texte.text(status)

        try:
            resultat = parse_pdf_document(file, filename,
                                         progress_callback=update_progress, mode=mode)
        except Exception as e:
            barre.empty()
            texte.empty()
            st.error(f"Erreur lors du traitement : {e}")
            return

        barre.empty()
        texte.empty()

        if resultat["run_id"]:
            st.session_state.last_parsed_data = {
                'filename': filename,
                'date': datetime.now().strftime("%d/%m/%Y %H:%M"),
                'projects': resultat["projects"],
                'run_id': resultat["run_id"],
                'document_id': resultat["document_id"],
                'resultat': resultat,
            }
        _afficher_bilan(resultat)


def _afficher_bilan(resultat):
    """
    Bilan d'un traitement. Le message dépend du statut : l'ancienne version
    affichait « traité avec succès » même quand des fiches manquaient.
    """
    for avertissement in resultat.get("avertissements", []):
        st.warning(avertissement)

    nb, echecs = len(resultat["projects"]), len(resultat["failures"])
    if resultat["statut"] == "en_attente":
        st.info(
            f"📨 Travail par lot soumis ({resultat.get('job_id')}). Les résultats "
            "sont à récolter depuis l'onglet **Historique** — vous pouvez fermer "
            "cette page en attendant."
        )
    elif resultat["statut"] == "echec":
        st.error("Aucune fiche n'a pu être extraite de ce document.")
    elif echecs:
        st.warning(f"⚠️ {nb} fiche(s) extraite(s), {echecs} en échec.")
    else:
        st.success(f"✅ Document traité — {nb} fiche(s) extraite(s).")

    if resultat.get("ocr_cache_hit"):
        st.caption("OCR servi depuis le cache : aucun appel OCR facturé.")
    _afficher_usage(resultat.get("usage"))


def _afficher_usage(usage):
    """Consommation du run, avec le taux de cache de prompt effectif."""
    if not usage:
        return
    extraction = usage.get("extraction") or {}
    if not extraction.get("appels"):
        return
    taux = pipeline.taux_cache(extraction)
    colonnes = st.columns(4)
    colonnes[0].metric("Appels d'extraction", extraction["appels"])
    colonnes[1].metric("Jetons de prompt", f"{extraction['prompt_tokens']:,}".replace(",", " "))
    colonnes[2].metric("Servis par le cache", f"{taux:.0%}",
                       help="Ces jetons sont facturés 10 % du tarif normal.")
    colonnes[3].metric("Jetons générés",
                       f"{extraction['completion_tokens']:,}".replace(",", " "))


def display_dashboard():
    """Display dashboard header"""
    st.markdown("""
    <div class="main-header">
        <h1>🌿 REX Zones Humides</h1>
        <h4>Extraction de retours d'expérience depuis PDF</h4>
    </div>
    """, unsafe_allow_html=True)



def _e(valeur):
    """
    Valeur prête à être interpolée dans du HTML.

    `format_expanded_data` construit une chaîne HTML consommée avec
    unsafe_allow_html : sans échappement, un champ contenant « <script> » — venu
    de l'OCR, ou d'une archive d'historique réimportée — devient du balisage
    actif. Les listes sont jointes ici : uniqueItems ayant été retiré du schéma,
    un tableau peut apparaître dans n'importe quelle section.
    """
    if valeur is None:
        return ""
    if isinstance(valeur, (list, tuple)):
        return html.escape(", ".join(str(v) for v in valeur))
    return html.escape(str(valeur))


def _url(valeur):
    """
    URL d'attribut href, restreinte à http(s).

    Bloque les schémas exécutables (javascript:, data:) que le schéma autorise
    déjà à ne pas produire, mais qu'une archive réimportée pourrait contenir.
    """
    texte = str(valeur or "").strip()
    if texte.lower().startswith(("http://", "https://")):
        return html.escape(texte, quote=True)
    return ""


def format_expanded_data(doc_data):
    """Format document data for expanded view based on new schema structure"""
    if not doc_data:
        return "Aucune donnée disponible"

    html_content = '<div class="expanded-content">'

    # Presentation info (capitalized key)
    if 'Presentation' in doc_data:
        pres_data = doc_data['Presentation']
        if isinstance(pres_data, dict) and any(pres_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>📋 Informations du projet</h4>'
            field_labels = {
                'Titre': 'Titre',
                'Bassin': 'Bassin',
                "Nom de l'organisme": 'Nom de l\'organisme',
                'Localisation': 'Localisation',
                'Adresse précise': 'Adresse précise',
                'Région': 'Région'
            }
            for key, label in field_labels.items():
                value = pres_data.get(key, '')
                if value:
                    html_content += f'<div class="field-item"><span class="field-label">{_e(label)}:</span><span class="field-value">{_e(value)}</span></div>'
            html_content += '</div>'

    # Objectif (capitalized key)
    if 'Objectif' in doc_data:
        obj_data = doc_data['Objectif']
        if isinstance(obj_data, dict) and obj_data.get('objectifs'):
            html_content += '<div class="field-group">'
            html_content += '<h4>🎯 Objectif du maître d\'ouvrage</h4>'
            html_content += f'<div class="field-item"><span class="field-value">{_e(obj_data["objectifs"])}</span></div>'
            html_content += '</div>'

    # Description (capitalized key)
    if 'Description' in doc_data:
        desc_data = doc_data['Description']
        if isinstance(desc_data, dict) and any(desc_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>📝 Description</h4>'
            if desc_data.get('resume'):
                html_content += f'<div class="field-item"><span class="field-label">Résumé:</span><span class="field-value">{_e(desc_data["resume"][:500])}{"..." if len(desc_data["resume"]) > 500 else ""}</span></div>'
            if desc_data.get('publication_recueil'):
                html_content += f'<div class="field-item"><span class="field-label">Publication:</span><span class="field-value">{_e(desc_data["publication_recueil"])}</span></div>'
            html_content += '</div>'

    # Enjeux (capitalized key)
    if 'Enjeux' in doc_data:
        enjeux_data = doc_data['Enjeux']
        if isinstance(enjeux_data, dict) and any(enjeux_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>🌱 Enjeux eau, biodiversité et climat</h4>'
            if enjeux_data.get('date_debut'):
                html_content += f'<div class="field-item"><span class="field-label">Date début:</span><span class="field-value">{_e(enjeux_data["date_debut"])}</span></div>'
            if enjeux_data.get('date_fin'):
                html_content += f'<div class="field-item"><span class="field-label">Date fin:</span><span class="field-value">{_e(enjeux_data["date_fin"])}</span></div>'
            if enjeux_data.get('enjeux') and isinstance(enjeux_data['enjeux'], list):
                html_content += f'<div class="field-item"><span class="field-label">Enjeux:</span><span class="field-value">{_e(", ".join(str(v) for v in enjeux_data["enjeux"]))}</span></div>'
            html_content += '</div>'

    # Typologie (capitalized key)
    if 'Typologie' in doc_data:
        typo_data = doc_data['Typologie']
        if isinstance(typo_data, dict) and any(typo_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>🔧 Typologie - Ingénierie écologique</h4>'
            # Changed to handle flat dict structure
            for key, value in typo_data.items():
                if value and value != "":
                    formatted_key = key.replace('_', ' ').title()
                    html_content += f'<div class="field-item"><span class="field-label">{_e(formatted_key)}:</span><span class="field-value">{_e(value)}</span></div>'
            html_content += '</div>'

    # Directives européennes (capitalized key)
    if 'Directives' in doc_data:
        dir_data = doc_data['Directives']
        if isinstance(dir_data, dict) and any(dir_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>🇪🇺 Référence directives européennes</h4>'
            for key, value in dir_data.items():
                if value and value != "":
                    formatted_key = key.replace('_', ' ').title()
                    html_content += f'<div class="field-item"><span class="field-label">{_e(formatted_key)}:</span><span class="field-value">{_e(value)}</span></div>'
            html_content += '</div>'

    # Contexte réglementaire (capitalized key)
    if 'Contexte' in doc_data:
        ctx_data = doc_data['Contexte']
        if isinstance(ctx_data, dict) and any(ctx_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>⚖️ Contexte réglementaire</h4>'
            if ctx_data.get('contexte'):
                html_content += f'<div class="field-item"><span class="field-label">Contexte:</span><span class="field-value">{_e(ctx_data["contexte"])}</span></div>'
            if ctx_data.get('autres'):
                html_content += f'<div class="field-item"><span class="field-label">Autres:</span><span class="field-value">{_e(ctx_data["autres"])}</span></div>'
            html_content += '</div>'

    # Valorisation (capitalized key)
    if 'Valorisation' in doc_data:
        val_data = doc_data['Valorisation']
        if isinstance(val_data, dict) and any(val_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>🏆 Valorisation de l\'opération</h4>'
            for key, value in val_data.items():
                if value and value != "":
                    formatted_key = key.replace('_', ' ').title()
                    if key == 'url':
                        html_content += f'<div class="field-item"><span class="field-label">{_e(formatted_key)}:</span><span class="field-value"><a href="{_url(value)}" target="_blank">{_e(value)}</a></span></div>'
                    else:
                        html_content += f'<div class="field-item"><span class="field-label">{_e(formatted_key)}:</span><span class="field-value">{_e(value)}</span></div>'
            html_content += '</div>'

    # Travaux (capitalized key)
    if 'Travaux' in doc_data:
        travaux_data = doc_data['Travaux']
        if isinstance(travaux_data, dict) and travaux_data.get('surface_travaux'):
            html_content += '<div class="field-group">'
            html_content += '<h4>🗺️ Période et envergure des travaux</h4>'
            html_content += f'<div class="field-item"><span class="field-label">Surface des travaux:</span><span class="field-value">{_e(travaux_data["surface_travaux"])}</span></div>'
            html_content += '</div>'

    # Documents (capitalized key)
    if 'Documents' in doc_data:
        doc_info = doc_data['Documents']
        if isinstance(doc_info, dict) and any(doc_info.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>📚 Documents</h4>'
            if doc_info.get('pages_extraire'):
                html_content += f'<div class="field-item"><span class="field-label">Pages à extraire:</span><span class="field-value">{_e(doc_info["pages_extraire"])}</span></div>'
            if doc_info.get('recueil_complet'):
                html_content += f'<div class="field-item"><span class="field-label">Recueil complet:</span><span class="field-value"><a href="{_url(doc_info["recueil_complet"])}" target="_blank">{_e(doc_info["recueil_complet"])}</a></span></div>'
            html_content += '</div>'

    html_content += '</div>'
    return html_content



MODES = {
    "rapide": "⚡ Rapide — résultats immédiats",
    "economique": "🐢 Économique — 50 % moins cher, résultats différés",
}


def display_file_upload():
    """Dépôt d'un PDF et choix du mode de traitement."""
    st.markdown("### 📤 Importer un nouveau document PDF")

    uploaded_file = st.file_uploader(
        "Sélectionnez un fichier PDF",
        type=['pdf'],
        help="Glissez-déposez votre fichier PDF ici ou cliquez pour parcourir"
    )

    if uploaded_file is None:
        return

    contenu = uploaded_file.getvalue()
    sha = pipeline.sha256_fichier(contenu)

    mode = st.radio(
        "Mode de traitement",
        options=list(MODES),
        format_func=lambda m: MODES[m],
        horizontal=True,
        key="mode_traitement",
        help=(
            "Rapide : les fiches sont extraites en parallèle et s'affichent tout "
            "de suite. Économique : l'extraction part en traitement par lot chez "
            "Mistral (moitié prix), les résultats se récoltent plus tard depuis "
            "l'onglet Historique — vous pouvez fermer la page."
        ),
    )

    col1, col2 = st.columns([1, 4])
    with col1:
        # Garde-fou d'interface contre le double clic. Le vrai garde-fou est en
        # base (index partiel « un seul run en cours par document ») : deux
        # onglets ne peuvent pas facturer deux fois le même PDF.
        deja = st.session_state.get("_dernier_envoi")
        if st.button("📤 Envoyer", key="upload_btn",
                     disabled=(deja == (sha, mode))):
            st.session_state["_dernier_envoi"] = (sha, mode)
            process_uploaded_file(contenu, uploaded_file.name, mode=mode)

    with col2:
        st.info(
            f"Fichier sélectionné : {uploaded_file.name} "
            f"({uploaded_file.size / 1024:.0f} Ko)"
        )
        if store.has_ocr_payload_pour_sha(sha):
            st.caption(
                "Ce document a déjà été océrisé : l'OCR sera repris du cache, "
                "sans nouvel appel facturé."
            )


# Sections du schéma, dans l'ordre d'apparition dans REX.schema.json.
SECTIONS = (
    'Presentation', 'Objectif', 'Description', 'Enjeux', 'Typologie',
    'Directives', 'Contexte', 'Valorisation', 'Travaux', 'Documents',
)

# Métadonnées de traçabilité reportées dans l'export. Les versions de modèle en
# font partie : elles étaient enregistrées par le pipeline mais n'atteignaient
# jamais l'Excel, ce qui rendait deux exports incomparables.
META_EXPORT = (
    '_project_title', '_page_debut', '_page_fin', '_segment_index',
    '_model_ocr', '_model_segmentation', '_model_extraction', '_prompt_hash',
)


def flatten_project_data(project):
    """
    Aplatit une fiche en un dict à un seul niveau pour l'export Excel.

    Les listes sont jointes pour TOUTES les sections. La version précédente ne
    le faisait que dans la branche Enjeux, si bien qu'un tableau venu d'une autre
    section (type_valorisation, notamment) arrivait brut jusqu'à xlsxwriter et
    ressortait en « ['Document de communications'] », crochets compris.

    Limite connue : les clés sont aplaties à leur nom feuille, sans préfixe de
    section — deux sections partageant un nom de champ s'écraseraient.
    """
    flat_data = {}
    for section in SECTIONS:
        contenu = project.get(section)
        if not isinstance(contenu, dict):
            continue
        for key, value in contenu.items():
            flat_data[key] = (
                ", ".join(str(v) for v in value) if isinstance(value, list) else value
            )

    for cle in META_EXPORT:
        if cle in project:
            flat_data[cle] = project[cle]

    return flat_data


def create_excel_download(projects):
    """Classeur Excel des fiches, en octets."""
    df = pd.DataFrame([flatten_project_data(project) for project in projects])
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='REX')
    return output.getvalue()


def _nom_export(filename):
    """Nom de fichier d'export, sans l'extension d'origine.

    `.replace('.pdf', '')` était sensible à la casse : IFD_….PDF donnait
    « IFD_….PDF_REX_export.xlsx ».
    """
    base = filename or "export"
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return f"{base}_REX_export.xlsx"


def display_results_table():
    """Display table with parsed results from last upload"""
    if 'last_parsed_data' not in st.session_state:
        return

    data = st.session_state.last_parsed_data
    projects = data.get('projects', [])

    if not projects:
        return

    st.markdown("---")
    st.markdown(f"### 📊 Résultats de l'analyse - {data['filename']}")
    st.markdown(f"**{len(projects)} projet(s) extrait(s)** - {data['date']}")

    # Génération différée : le classeur n'est construit qu'au clic. Il l'était
    # jusqu'ici à chaque rerun, que l'utilisateur télécharge ou non.
    st.download_button(
        label="📥 Télécharger en Excel",
        data=functools.partial(create_excel_download, projects),
        file_name=_nom_export(data['filename']),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Télécharger les données extraites au format Excel",
        key="dl_excel_resultats",
    )

    # Prepare DataFrame for st_mui_table
    df_data = []
    for project in projects:
        titre = project.get("_project_title", "Sans titre")
        page_debut = project.get("_page_debut", "N/A")
        page_fin = project.get("_page_fin", "N/A")
        
        df_data.append({
            "Titre du projet": titre,
            "Page début": page_debut,
            "Page fin": page_fin,
            "Détails": format_expanded_data(project)
        })
    
    df = pd.DataFrame(df_data)
    
    # Display table with expandable details
    try:
        st_mui_table(
            df,
            customCss="""
/* Material 3 Expressive - Water & Biodiversity Theme */
:root {
    /* Primary - Deep Ocean */
    --md-sys-color-primary: #006A6B;
    --md-sys-color-on-primary: #FFFFFF;
    --md-sys-color-primary-container: #9CF0F2;
    --md-sys-color-on-primary-container: #002020;
    
    /* Secondary - Wetland Green */
    --md-sys-color-secondary: #4A6363;
    --md-sys-color-on-secondary: #FFFFFF;
    --md-sys-color-secondary-container: #CCE8E8;
    --md-sys-color-on-secondary-container: #051F1F;
    
    /* Tertiary - Fresh Water */
    --md-sys-color-tertiary: #00838F;
    --md-sys-color-on-tertiary: #FFFFFF;
    
    /* Surface & Background */
    --md-sys-color-surface: #FAFDFC;
    --md-sys-color-on-surface: #191C1C;
    --md-sys-color-surface-variant: #DAE5E4;
    --md-sys-color-surface-container-low: #F0F4F4;
    --md-sys-color-surface-container: #E6EBEB;
    --md-sys-color-surface-container-high: #DFE4E4;
    
    /* Outline & Borders */
    --md-sys-color-outline: #6F7979;
    --md-sys-color-outline-variant: #BEC8C8;
    
    /* Biodiversity Accent Colors */
    --bio-green: #2E7D32;
    --bio-blue: #0277BD;
    --bio-teal: #00695C;
    
    /* Elevation & Shadow */
    --elevation-1: 0 1px 3px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 106, 107, 0.06);
    --elevation-2: 0 3px 6px rgba(0, 0, 0, 0.1), 0 2px 4px rgba(0, 106, 107, 0.08);
    --elevation-3: 0 6px 12px rgba(0, 0, 0, 0.12), 0 4px 8px rgba(0, 106, 107, 0.1);
    --elevation-4: 0 12px 24px rgba(0, 0, 0, 0.14), 0 8px 16px rgba(0, 106, 107, 0.12);
    
    /* Expressive Radii */
    --radius-small: 12px;
    --radius-medium: 20px;
    --radius-large: 28px;
    --radius-extra-large: 36px;
    
    /* Transitions */
    --transition-standard: cubic-bezier(0.4, 0.0, 0.2, 1);
    --transition-decelerate: cubic-bezier(0.0, 0.0, 0.2, 1);
    --transition-accelerate: cubic-bezier(0.4, 0.0, 1, 1);
}

/* Table styling - M3 Expressive */
.MuiTableContainer-root {
    border-radius: var(--radius-large) !important;
    box-shadow: var(--elevation-2) !important;
    background: var(--md-sys-color-surface) !important;
    overflow: hidden !important;
}

.MuiTableHead-root {
    background: linear-gradient(135deg, var(--md-sys-color-primary-container) 0%, var(--md-sys-color-secondary-container) 100%) !important;
}

.MuiTableHead-root th {
    color: var(--md-sys-color-on-primary-container) !important;
    font-weight: 600 !important;
    font-size: 0.9375rem !important;
    letter-spacing: 0.5px !important;
    padding: 1.25rem 1rem !important;
}

.MuiTableBody-root tr {
    transition: all 0.2s var(--transition-standard) !important;
}

.MuiTableBody-root tr:hover {
    background: var(--md-sys-color-surface-container-low) !important;
    transform: translateX(2px) !important;
}

.MuiTableCell-root {
    border-bottom: 1px solid var(--md-sys-color-outline-variant) !important;
    padding: 1rem !important;
}

.MuiTablePagination-root {
    border-top: 2px solid var(--md-sys-color-primary-container) !important;
}

.MuiTablePagination-toolbar > p {
    margin: 0 !important;
    font-weight: 500 !important;
    color: var(--md-sys-color-on-surface) !important;
}

.MuiIconButton-root {
    color: var(--md-sys-color-primary) !important;
    transition: all 0.2s var(--transition-standard) !important;
}

.MuiIconButton-root:hover {
    background: var(--md-sys-color-primary-container) !important;
    transform: scale(1.1) !important;
}


                td.MuiTableCell-sizeSmall:first-child {
                    display: none;
                }

                .expanded-content .field-group {
                    padding: 1rem 0;
                }


            """,
            detailColumns=["Détails"],
            detailColNum=1,
            detailsHeader="",
            paginationSizes=[10, 25, 50],
            paginationLabel="Projets par page",
            enablePagination=True,
            showIndex=False,
            size="medium",
            stickyHeader=True,
            enable_sorting=False,
            # Sans key=, le composant se remonte à chaque rerun et perd les
            # lignes dépliées.
            key="tableau_resultats",
        )
    except Exception as e:
        st.error(f"Erreur d'affichage du tableau : {e}")
        # Repli sur des composants Streamlit natifs. Il lisait « presentation »
        # et « titre » en minuscules alors que le schéma produit « Presentation »
        # / « Titre » : la recherche échouait toujours et chaque ligne
        # s'appelait « Projet N », alors que _project_title était disponible.
        for idx, project in enumerate(projects):
            titre = (
                project.get("_project_title")
                or (project.get("Presentation") or {}).get("Titre")
                or f"Projet {idx + 1}"
            )
            page_debut = project.get("_page_debut", "N/A")
            page_fin = project.get("_page_fin", "N/A")

            with st.expander(f"📄 {titre} (Pages {page_debut}-{page_fin})"):
                st.markdown(format_expanded_data(project), unsafe_allow_html=True)


def display_failures_panel():
    """
    Fiches en échec du dernier traitement, avec relance.

    Ces échecs partaient auparavant dans un print() vers la sortie du serveur :
    l'interface annonçait « traité avec succès » avec des fiches en moins.
    """
    data = st.session_state.get('last_parsed_data') or {}
    resultat = data.get('resultat') or {}
    echecs = resultat.get('failures')
    if echecs is None and data.get('run_id'):
        echecs = store.load_failures(data['run_id'])
    if not echecs:
        return

    with st.expander(f"⚠️ {len(echecs)} fiche(s) en échec", expanded=True):
        for echec in echecs:
            debut, fin = (echec.get('pages') or (None, None))[:2]
            st.markdown(
                f"**{echec.get('titre') or 'Sans titre'}** — pages {debut}-{fin} · "
                f"`{echec.get('categorie') or 'inconnu'}`"
            )
            st.caption(echec.get('error') or "")
            if echec.get('trace'):
                with st.popover("Trace"):
                    st.code(echec['trace'])

        relancables = [e['index'] for e in echecs if e.get('reessayable', True)]
        if relancables and data.get('document_id'):
            st.caption(
                "La relance repart de l'OCR en cache : ni téléversement, ni OCR, "
                "ni nouveau découpage."
            )
            if st.button(f"🔁 Relancer les {len(relancables)} fiche(s) relançable(s)",
                         key="relancer_echecs"):
                barre, texte = st.progress(0), st.empty()
                bilan = relancer_fiches(
                    data['document_id'], data['run_id'], relancables,
                    progress_callback=lambda p, s, **_: (
                        barre.progress(max(0.0, min(1.0, p))), texte.text(s)),
                )
                if bilan:
                    _recharger_run(data['run_id'])
                    st.toast(
                        f"{bilan['relancees']} fiche(s) récupérée(s), "
                        f"{bilan['encore_en_echec']} encore en échec"
                    )
                    st.rerun()


def _recharger_run(run_id):
    """Charge un run en session pour que le tableau et l'export l'affichent."""
    data = store.load_run_as_parsed_data(run_id)
    if data is None:
        st.error("Traitement introuvable dans l'historique.")
        return False
    data['resultat'] = {'failures': store.load_failures(run_id)}
    st.session_state.last_parsed_data = data
    return True


def display_batch_jobs_panel():
    """
    Travaux par lot en attente, avec sondage manuel.

    Aucune boucle d'attente : un run de script détient le thread de la session et
    meurt avec le websocket. L'état de référence vit chez Mistral et en base, pas
    dans st.session_state — c'est ce qui permet de fermer l'onglet et de revenir.
    """
    travaux = store.open_batch_jobs()
    if not travaux:
        return

    st.markdown("#### 📨 Traitements par lot en attente")
    for travail in travaux:
        colonnes = st.columns([4, 2, 2])
        colonnes[0].markdown(
            f"**{travail['filename']}** · `{travail['job_id']}`  \n"
            f"soumis le {travail['created_at'].replace('T', ' ')[:16]}"
        )
        avancement = ""
        if travail.get('total_requests'):
            avancement = (f" · {travail.get('succeeded_requests') or 0}"
                          f"/{travail['total_requests']}")
        colonnes[1].markdown(f"`{travail['status']}`{avancement}")
        if colonnes[2].button("🔄 Actualiser", key=f"maj_{travail['job_id']}"):
            with st.spinner("Interrogation du traitement par lot…"):
                bilan = actualiser_travail_par_lot(travail['job_id'])
            if bilan and bilan.get('recolte'):
                _recharger_run(bilan['run_id'])
                message = f"{bilan['ok']} fiche(s) récoltée(s)"
                if bilan['echecs'] or bilan['manquants']:
                    message += (f", {bilan['echecs']} en échec, "
                                f"{bilan['manquants']} sans résultat")
                st.toast(message)
            elif bilan:
                st.toast(f"Toujours en cours : {bilan['statut']}")
            st.rerun()


def display_history():
    """Documents traités, leurs runs, et la sauvegarde de l'historique."""
    display_batch_jobs_panel()

    documents = store.list_documents()
    stats = store.historique_stats()
    st.markdown("#### 🗂️ Documents traités")
    st.caption(
        f"{stats['documents']} document(s), {stats['runs']} traitement(s), "
        f"{stats['fiches']} fiche(s) extraite(s) · "
        f"cache OCR {stats['ocr_bytes'] / 1e6:.1f} Mo"
    )

    if not documents:
        st.info("Aucun document dans l'historique pour le moment.")
    for doc in documents:
        titre = (f"📄 {doc['filename']} — {doc['nb_runs']} traitement(s)"
                 + (f", {doc['page_count']} pages" if doc['page_count'] else ""))
        with st.expander(titre):
            if doc['a_cache_ocr']:
                st.caption(
                    f"OCR en cache ({(doc['ocr_bytes'] or 0) / 1e6:.2f} Mo décompressés) "
                    "— un nouveau traitement de ce PDF ne repaiera pas l'OCR."
                )
            for run in store.list_runs(doc['id']):
                _afficher_run(run)
            _confirmer_suppression(doc)

    _display_sauvegarde_historique(stats)


def _afficher_run(run):
    """Une ligne de traitement, avec rechargement sans appel API."""
    colonnes = st.columns([3, 2, 2, 2])
    debut = (run['started_at'] or '').replace('T', ' ')[:16]
    colonnes[0].markdown(
        f"{debut} · `{run['mode']}` · **{run['status']}**  \n"
        f"{run['nb_ok'] or 0} fiche(s) OK"
        + (f", {run['nb_echec']} en échec" if run['nb_echec'] else "")
    )
    colonnes[1].caption(
        f"{run['model_extraction'] or '—'}  \nprompt `"
        f"{(run['prompt_extraction_sha256'] or '')[:8]}`"
    )
    if run['prompt_tokens']:
        taux = (run['cached_tokens'] or 0) / run['prompt_tokens']
        colonnes[2].caption(
            f"{run['prompt_tokens']:,}".replace(",", " ") + " jetons  \n"
            f"cache {taux:.0%}"
        )
    if colonnes[3].button("Rouvrir", key=f"rouvrir_{run['id']}",
                          disabled=not run['nb_ok']):
        if _recharger_run(run['id']):
            st.rerun()
    if run['error']:
        st.caption(f"⚠️ {run['error']}")


@st.dialog("Supprimer ce document ?")
def _dialogue_suppression(doc):
    st.write(
        f"**{doc['filename']}**, ses {doc['nb_runs']} traitement(s), leurs fiches "
        "et sa charge OCR seront supprimés définitivement."
    )
    st.caption("L'OCR devra être repayé si ce PDF est redéposé plus tard.")
    if st.button("Supprimer définitivement", type="primary",
                 key=f"confirmer_suppr_{doc['id']}"):
        store.delete_document(doc['id'])
        ouvert = st.session_state.get('last_parsed_data') or {}
        if ouvert.get('document_id') == doc['id']:
            st.session_state.pop('last_parsed_data', None)
        st.rerun()


def _confirmer_suppression(doc):
    if st.button("🗑️ Supprimer", key=f"suppr_{doc['id']}"):
        _dialogue_suppression(doc)


def _display_sauvegarde_historique(stats):
    """
    Sauvegarde et restauration de l'historique.

    Le stockage en ligne est éphémère : un redéploiement de Streamlit Community
    Cloud efface la base. L'archive est donc la seule façon de conserver un jeu
    de documents traités — et surtout leurs charges OCR, le seul coût non
    reproductible.
    """
    st.divider()
    st.markdown("#### 💾 Sauvegarde de l'historique")
    st.caption(
        "Le stockage de l'application est éphémère : un redéploiement efface "
        "l'historique. Téléchargez une archive pour pouvoir la réimporter ensuite."
    )

    col1, col2 = st.columns(2)
    with col1:
        inclure_ocr = st.toggle(
            "Inclure le cache OCR", value=True, key="export_inclure_ocr",
            help="Sans le cache OCR l'archive est bien plus légère, mais "
                 "réimporter un PDF repaiera son OCR.",
        )
        st.download_button(
            "⬇️ Télécharger l'archive",
            # Génération différée : l'archive n'est construite qu'au clic.
            data=functools.partial(_construire_archive, inclure_ocr),
            file_name=f"rex-historique-{datetime.now():%Y%m%d-%H%M}.zip",
            mime="application/zip",
            key="dl_archive",
            disabled=not stats['documents'],
            help=(f"≈ {stats['ocr_bytes'] / 1e6:.1f} Mo compressés"
                  if inclure_ocr else "Résultats seuls, sans le cache OCR"),
        )

    with col2:
        archive = st.file_uploader(
            "Restaurer une archive", type=['zip'], key="import_archive",
            help="Archive ZIP produite par le bouton ci-contre. N'importez que "
                 "des archives dont vous connaissez la provenance : une archive "
                 "tierce peut injecter de faux résultats OCR dans le cache.",
        )
        if archive is not None and st.button("⬆️ Restaurer", key="btn_import"):
            try:
                rapport = store.import_bundle(archive.getvalue())
            except store.BundleInvalide as err:
                st.error(f"Archive invalide : {err}")
            else:
                st.success(
                    f"{rapport['documents_ajoutes']} document(s) ajouté(s), "
                    f"{rapport['runs_ajoutes']} traitement(s), "
                    f"{rapport['fiches_ajoutees']} fiche(s), "
                    f"{rapport['ocr_ajoutes']} charge(s) OCR. "
                    f"{rapport['runs_ignores']} traitement(s) déjà présent(s)."
                )
                st.rerun()


def _construire_archive(inclure_ocr):
    return store.export_bundle(include_ocr=inclure_ocr,
                               mistralai_version=_version_mistralai())


def _display_maintenance():
    """Rechargement des prompts et des schémas depuis le disque."""
    with st.popover("⚙️ Maintenance"):
        st.caption(
            "Recharge les prompts Markdown et les schémas JSON depuis le disque. "
            "Nécessaire après avoir édité REXPrompt.md, listPrompt.md, "
            "REX.schema.json ou REXlist.schema.json : ils sont chargés une fois "
            "par session, donc un simple rerun ne suffit pas."
        )
        if st.button("♻️ Recharger prompts et schémas", key="recharger_prompts"):
            for cle in CLES_PROMPTS:
                st.session_state.pop(cle, None)
            load_text_file.clear()
            st.toast("Prompts et schémas rechargés")
            st.rerun()
        st.caption(f"Base d'historique : `{store.db_path() or get_db_path()}`")


def _charger_prompts_et_schemas():
    """
    Charge prompts et schémas en session, une fois par session.

    Leur empreinte est calculée à chaque run sur le prompt RENDU, donc une
    édition suivie d'un rechargement apparaît dans l'historique comme un
    traitement distinct.
    """
    if 'REXSchema' not in st.session_state:
        st.session_state.REXSchema = load_schema('REX.schema.json')
        if not st.session_state.REXSchema:
            st.error("Chargement de REX.schema.json impossible.")
            st.stop()

    if 'REXListSchema' not in st.session_state:
        st.session_state.REXListSchema = load_schema('REXlist.schema.json')
        if not st.session_state.REXListSchema:
            st.error("Chargement de REXlist.schema.json impossible.")
            st.stop()

    if 'REXPrompt' not in st.session_state:
        st.session_state.REXPrompt = load_prompt('REXPrompt.md',
                                                 st.session_state.REXSchema)
        if not st.session_state.REXPrompt:
            st.error("Chargement de REXPrompt.md impossible.")
            st.stop()

    if 'listPrompt' not in st.session_state:
        st.session_state.listPrompt = load_prompt('listPrompt.md',
                                                  st.session_state.REXListSchema)
        if not st.session_state.listPrompt:
            st.error("Chargement de listPrompt.md impossible.")
            st.stop()


def main():
    """Point d'entrée de l'application."""
    _charger_prompts_et_schemas()
    store.init_db(get_db_path())

    display_dashboard()

    # Deux onglets seulement : « Traitement » est l'écran existant, inchangé
    # dans son enchaînement. La refonte d'interface (tâche 4) transposera ces
    # corps d'onglet en écrans sans les réécrire.
    onglet_traitement, onglet_historique = st.tabs(["Traitement", "Historique"])

    with onglet_traitement:
        _display_maintenance()
        display_file_upload()
        display_failures_panel()
        display_results_table()

    with onglet_historique:
        display_history()


if __name__ == "__main__":
    main()


