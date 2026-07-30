"""
Cœur du traitement REX-MH : client Mistral, construction des requêtes,
concurrence, mode par lot, classification des erreurs, comptabilité des jetons.

Ce module n'importe PAS streamlit, et ce n'est pas une convention de style.
Un thread de travail sans ScriptRunContext qui lit `st.session_state` ne reçoit
pas une erreur claire : Streamlit lui sert un SessionState factice GLOBAL au
processus, si bien que les N fiches échouent identiquement sur un
`AttributeError` et que l'interface n'affiche qu'« aucun projet analysé ».
Rendre `st` inaccessible ici empêche mécaniquement ce bug de revenir.

Les prompts, schémas et la clé API arrivent donc en arguments. `app.py` reste
seul responsable de `st.secrets`, du cache de ressources et de l'affichage.
"""
import hashlib
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from mistralai.client import Mistral
from mistralai.client.errors import (
    HTTPValidationError,
    MistralError,
    NoResponseError,
    ResponseValidationError,
)
from mistralai.client.utils import BackoffStrategy, RetryConfig

# --- Modèles Mistral ---------------------------------------------------------
# On appelle les alias "-latest" pour suivre les montées de version, et on
# enregistre la version réellement résolue par l'API à chaque appel (voir
# _resolved_model) afin que deux extractions restent comparables.
MODEL_OCR = "mistral-ocr-4-0"                # OCR 4 : blocs structurels + confiance
MODEL_EXTRACTION = "mistral-medium-latest"   # Medium 3.5 : le plus précis
MODEL_SEGMENTATION = "mistral-small-latest"  # Small 4 : rapide, suffisant pour découper

# Graine fixe : deux exécutions sur le même document doivent donner le même
# résultat. temperature=0.0 + graine + json_schema strict sont délibérés
# (reproductibilité, propreté des énumérations) — ne pas les assouplir.
RANDOM_SEED = 20260729

# Paramètres d'appel OCR. Regroupés ici parce qu'ils entrent dans la clé du
# cache OCR : basculer include_blocks sans changer la clé servirait une charge
# sans blocs à une segmentation qui les attend.
OCR_PARAMS = {
    "include_image_base64": False,
    # Blocs structurels (titre / texte / tableau…) en ordre de lecture, avec
    # coordonnées. Serviront de candidats de découpe entre fiches projet.
    "include_blocks": True,
    # En-têtes et pieds sortis du corps de texte : ils polluaient la segmentation.
    "extract_header": True,
    "extract_footer": True,
    # Confiance par page : repérer les pages mal océrisées avant de blâmer le
    # prompt d'extraction.
    "confidence_scores_granularity": "page",
    "table_format": "markdown",
}

# 4 extractions en vol ≈ 48 000 jetons de prompt : au-dessus, on court après le
# quota sans gagner de temps de mur.
MAX_CONCURRENCE_EXTRACTION = 4

# Le SDK retombe sur 300 s par opération quand timeout_ms n'est pas passé. Avec
# 4 workers, UN appel pendu immobiliserait 25 % du débit pendant cinq minutes.
TIMEOUT_UPLOAD_MS = 180_000        # 9,4 Mo à téléverser
TIMEOUT_OCR_MS = 600_000           # un recueil de 130 pages met légitimement des minutes
TIMEOUT_SEGMENTATION_MS = 120_000
TIMEOUT_EXTRACTION_MS = 120_000

# Le défaut du SDK est AUCUN retry : un seul 429 perdait une fiche. Le backoff
# intégré honore déjà l'en-tête Retry-After — ne pas écrire de boucle maison.
# Passé par appel et non sur le client : chaque jambe veut un budget différent
# (réessayer quatre fois un téléversement de 9,4 Mo, c'est 38 Mo montés).
RETRY_EXTRACTION = RetryConfig(
    "backoff",
    BackoffStrategy(initial_interval=1_000, max_interval=20_000, exponent=1.6,
                    max_elapsed_time=120_000),
    True,  # retry_connection_errors : une coupure réseau ne doit pas perdre une fiche
)
RETRY_SEGMENTATION = RetryConfig(
    "backoff",
    BackoffStrategy(initial_interval=1_000, max_interval=20_000, exponent=1.6,
                    max_elapsed_time=120_000),
    True,
)
RETRY_OCR = RetryConfig(
    "backoff",
    BackoffStrategy(initial_interval=2_000, max_interval=20_000, exponent=2.0,
                    max_elapsed_time=60_000),
    True,
)
RETRY_UPLOAD = RetryConfig(
    "backoff",
    BackoffStrategy(initial_interval=2_000, max_interval=15_000, exponent=2.0,
                    max_elapsed_time=60_000),
    True,
)

# Poids des phases dans la barre de progression (somme = 1.0).
POIDS_PHASES = {"upload": 0.05, "ocr": 0.25, "segmentation": 0.10, "extraction": 0.60}

# Mots-clés JSON Schema que le mode strict de Mistral refuse (erreur 3051,
# « Invalid structured output syntax »), vérifiés un par un contre l'API :
#   - anyOf / oneOf / allOf / not : pas d'union ni de composition
#   - format ("uri", "date"…)     : non reconnu
#   - uniqueItems                 : non reconnu
# En revanche pattern, maxLength, minimum, examples, enum (même 53 valeurs),
# tableaux d'enum et objets imbriqués passent sans problème.
UNSUPPORTED_SCHEMA_KEYWORDS = (
    "anyOf", "oneOf", "allOf", "not", "format", "uniqueItems", "$ref", "if", "then", "else",
)


# --- Client ------------------------------------------------------------------


def construire_client(api_key):
    """
    Client Mistral partagé par tout le processus, prêt pour la concurrence.

    `httpx.Client` est thread-safe et ses limites par défaut (max_connections=100)
    sont très au-dessus de nos quelques workers. Un client réutilisé conserve ses
    connexions keep-alive d'un traitement au suivant.

    Les sous-SDK sont construits paresseusement dans `Mistral.__getattr__`, et
    `dynamic_import` fait un `sys.modules.pop()` sur KeyError : deux threads qui
    déclenchent l'import en même temps peuvent se voir servir un module à moitié
    initialisé. On les force donc ici, sur le thread appelant, avant tout submit.
    """
    client = Mistral(api_key=api_key)
    _ = client.chat, client.ocr, client.files, client.batch.jobs
    return client


# --- Schémas et requêtes -----------------------------------------------------


def strict_schema(node):
    """
    Rend un schéma JSON acceptable par le mode strict de l'API Mistral :
      1. tout objet interdit les propriétés supplémentaires ;
      2. les mots-clés non supportés sont retirés.

    Les schémas du dépôt sont déjà conformes ; cette passe est une ceinture de
    sécurité pour qu'un ajout de champ malheureux dégrade la validation au lieu
    de faire échouer tout le traitement avec une erreur 400 peu parlante.
    """
    if isinstance(node, dict):
        cleaned = {
            k: strict_schema(v)
            for k, v in node.items()
            if k not in UNSUPPORTED_SCHEMA_KEYWORDS
        }
        if cleaned.get("type") == "object":
            cleaned["additionalProperties"] = False
        return cleaned
    if isinstance(node, list):
        return [strict_schema(v) for v in node]
    return node


def json_schema_format(name, schema):
    """
    Construire le response_format "json_schema" strict attendu par l'API Mistral.

    Le modèle ne peut ni inventer une clé hors schéma ni sortir d'une
    énumération, ce qui supprime à la source les dérives constatées côté client
    (« Natura 2000 » au lieu de « Site Natura 2000 », singulier/pluriel Ramsar).
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": strict_schema(json.loads(json.dumps(schema))),
            "strict": True,
        },
    }


def empreinte(texte):
    """Empreinte hexadécimale d'un prompt rendu ou d'un schéma sérialisé."""
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


def cle_cache_prompt(prefixe, prompt_rendu, modele):
    """
    Valeur de `prompt_cache_key` (absent de ocr.process).

    Le cache de prompt Mistral est OPT-IN : sans cette clé aucun préfixe n'est
    écrit — mesuré, `cached_tokens: 0` sur les ~12 000 jetons de prompt par
    fiche. La clé augmente la chance d'un hit sans la garantir : un préfixe de
    jetons identique reste requis, et il l'est déjà (le message système rendu est
    octet-pour-octet le même pour toutes les fiches de tous les documents, et le
    contenu variable est un message `user` SÉPARÉ, placé après).

    Y figurent l'empreinte du prompt RENDU (schéma inliné compris) et le modèle,
    les caches n'étant pas partagés entre tiers. N'y figurent PAS le document,
    l'index de fiche ni le run : ce serait un seau par appel, donc 0 % de
    réutilisation. Une clé agnostique du document fait démarrer le recueil
    suivant déjà chaud.

    L'empreinte du prompt est indispensable : le schéma EST du payload de prompt
    (33 060 des 35 080 caractères du prompt rendu). Sans elle, éditer
    REXPrompt.md ou REX.schema.json laisserait un seau chaud côté serveur dont
    les jetons ne correspondent plus — manque garanti à chaque appel, et plus
    aucun moyen de distinguer un manque « prompt modifié » d'un échauffement
    raté quand on mesure.

    On prend l'ALIAS de modèle, pas la version résolue : celle-ci n'est connue
    qu'après la première réponse, et l'utiliser ferait différer la clé de la
    fiche 0 de celle des fiches suivantes, ce qui détruirait l'échauffement.
    """
    return f"rex-{prefixe}-{modele}-{empreinte(prompt_rendu)[:16]}"


def construire_requete_chat(prompt_systeme, contenu, *, modele, response_format,
                            prompt_cache_key):
    """
    Construit LE dict de requête chat partagé par les deux modes.

    Ses clés sont à la fois les paramètres nommés de `chat.complete` et les clés
    du corps JSON d'une requête par lot, donc le même dict sert :
      - mode « rapide »     : client.chat.complete(**requete)
      - mode « économique » : {"custom_id": …, "body": requete}
    """
    return {
        "model": modele,
        "messages": [
            {"role": "system", "content": prompt_systeme},
            {"role": "user", "content": contenu},
        ],
        "temperature": 0.0,
        "random_seed": RANDOM_SEED,
        "response_format": response_format,
        "prompt_cache_key": prompt_cache_key,
    }


def _resolved_model(response, fallback):
    """Version de modèle réellement servie par l'API (pour la traçabilité)."""
    return getattr(response, "model", None) or fallback


# --- Pages OCR ---------------------------------------------------------------


def _ocr_pages(ocr_response):
    """
    Pages OCR normalisées en (numéro 1-indexé, markdown), que la source soit une
    réponse OCR vivante ou une charge rechargée depuis le cache SQLite (un dict).

    Les numéros de page sont 1-indexés PARTOUT sauf dans la réponse OCR, où
    `page.index` est 0-based : la conversion se fait ici, et nulle part ailleurs.
    """
    pages = ocr_response["pages"] if isinstance(ocr_response, dict) else ocr_response.pages
    for page in pages or []:
        if isinstance(page, dict):
            yield page["index"] + 1, page.get("markdown") or ""
        else:
            yield page.index + 1, page.markdown or ""


def clean_document(ocr_response):
    """Document complet au format {"pages": [{page_number, content}]}."""
    return json.dumps(
        {"pages": [{"page_number": n, "content": c} for n, c in _ocr_pages(ocr_response)]},
        indent=2,
    )


def clean_pages(ocr_response, start_page, end_page):
    """Sous-ensemble de pages (bornes 1-indexées, incluses)."""
    return json.dumps(
        {
            "pages": [
                {"page_number": n, "content": c}
                for n, c in _ocr_pages(ocr_response)
                if start_page <= n <= end_page
            ]
        },
        indent=2,
    )


def nombre_de_pages(ocr_response):
    pages = ocr_response["pages"] if isinstance(ocr_response, dict) else ocr_response.pages
    return len(pages or [])


def confiance_moyenne(ocr_response):
    """Moyenne des scores de confiance par page, ou None si non demandés."""
    pages = ocr_response["pages"] if isinstance(ocr_response, dict) else ocr_response.pages
    scores = []
    for page in pages or []:
        conf = page.get("confidence_scores") if isinstance(page, dict) else getattr(
            page, "confidence_scores", None
        )
        if conf is None:
            continue
        valeur = (
            conf.get("average_page_confidence_score")
            if isinstance(conf, dict)
            else getattr(conf, "average_page_confidence_score", None)
        )
        if isinstance(valeur, (int, float)):
            scores.append(float(valeur))
    return sum(scores) / len(scores) if scores else None


def cle_cache_ocr(contenu, modele_ocr=MODEL_OCR, params=None):
    """
    Clé du cache OCR : le fichier ET les paramètres d'appel ET le modèle.

    Le hash du fichier seul ne suffit pas — basculer `include_blocks` ou la
    granularité des scores de confiance servirait une charge périmée à un
    appelant qui attend autre chose.
    """
    params = OCR_PARAMS if params is None else params
    h_params = hashlib.sha256(
        json.dumps(params, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    return f"{hashlib.sha256(contenu).hexdigest()}-{modele_ocr}-{h_params}"


def sha256_fichier(contenu):
    return hashlib.sha256(contenu).hexdigest()


# --- Comptabilité des jetons -------------------------------------------------


def jetons_caches(usage):
    """
    Jetons servis depuis le cache de prompt.

    PIÈGE : `UsageInfo` ne déclare QUE prompt_tokens, completion_tokens,
    total_tokens et prompt_audio_seconds. Le modèle est `extra="allow"`, donc
    `prompt_tokens_details` arrive en DICT BRUT :
        usage.prompt_tokens_details["cached_tokens"]   -> OK
        usage.prompt_tokens_details.cached_tokens      -> AttributeError
    Une classe `PromptTokensDetails` typée existe dans le SDK mais aucun modèle
    de réponse ne la référence — vestige mort, ne pas s'y fier.
    """
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if isinstance(details, dict):
        return int(details.get("cached_tokens") or 0)
    return int(getattr(details, "cached_tokens", 0) or 0)


def usage_vide():
    return {"appels": 0, "prompt_tokens": 0, "cached_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0}


def usage_depuis_reponse(reponse):
    u = getattr(reponse, "usage", None)
    return {
        "appels": 1,
        "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
        "cached_tokens": jetons_caches(u),
        "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
    }


def usage_cumuler(total, ajout):
    for cle, valeur in (ajout or {}).items():
        total[cle] = total.get(cle, 0) + valeur
    return total


def taux_cache(usage):
    """Part des jetons de prompt servie par le cache (0.0 si aucun appel)."""
    prompt = (usage or {}).get("prompt_tokens", 0)
    return ((usage or {}).get("cached_tokens", 0) / prompt) if prompt else 0.0


def usage_neuf():
    """Structure d'usage d'un run. L'OCR se compte en pages, pas en jetons."""
    return {
        "ocr": {"pages_traitees": 0, "octets": 0},
        "segmentation": usage_vide(),
        "extraction": usage_vide(),
        "total": usage_vide(),
    }


# --- Erreurs -----------------------------------------------------------------


def classer_erreur(exc):
    """
    (catégorie, réessayable, code HTTP).

    Ordre important : HTTPValidationError et SDKError dérivent tous deux de
    MistralError, donc les sous-classes d'abord. `NoResponseError` n'est PAS une
    MistralError (elle hérite d'Exception) : elle a sa branche. Il n'existe
    aucune exception 429 dédiée, d'où la discrimination sur status_code.

    Une erreur 400/422 n'est JAMAIS réessayable : c'est notre schéma ou notre
    prompt qui est invalide, réessayer brûlerait des jetons pour le même échec.
    Cela couvre le 3051 « Invalid structured output syntax », qui arrive en 400.
    """
    if isinstance(exc, json.JSONDecodeError):
        return "json_invalide", True, None      # sortie tronquée : réessayable
    if isinstance(exc, HTTPValidationError):
        return "requete", False, 422
    if isinstance(exc, ResponseValidationError):
        return "sdk_desync", False, None        # réponse valide, SDK en retard
    if isinstance(exc, MistralError):
        code = getattr(exc, "status_code", None)
        if code == 429:
            return "quota", True, code
        if code in (401, 403):
            return "auth", False, code
        if code is not None and code >= 500:
            return "serveur", True, code
        if code is not None and code >= 400:
            return "requete", False, code
        return "inconnu", True, code
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", True, None
    if isinstance(exc, (httpx.NetworkError, NoResponseError)):
        return "reseau", True, None
    if isinstance(exc, (AttributeError, NameError, ImportError, TypeError)):
        # Pas un problème de données : un bug de notre côté. Typiquement
        # l'AttributeError qu'un thread sans ScriptRunContext obtient en lisant
        # st.session_state — Streamlit lui sert un état factice au lieu de lever
        # « pas de contexte », si bien que TOUTES les fiches échouent pareil.
        # Catégorie distincte pour que l'échauffement puisse avorter le run au
        # lieu de payer N fois le même bug.
        return "bug", False, None
    return "inconnu", False, None


def _echec(index, titre, debut, fin, categorie, message, reessayable, code=None,
           trace=None):
    return {
        "index": index,
        "titre": titre,
        "pages": (debut, fin),
        "categorie": categorie,
        "error": message,
        "reessayable": reessayable,
        "status_code": code,
        "trace": trace,
    }


# --- Segments ----------------------------------------------------------------


def valider_segment(segment, nb_pages):
    """
    Renvoie (debut, fin) en 1-indexé, ou un message d'erreur (str).

    Corrige le `if not start_page` de la version précédente, qui confondait
    PageDebut == 0 avec une clé absente et abandonnait la fiche dans un print()
    vers la sortie du serveur. `REXlist.schema.json` n'impose pas de `minimum`,
    donc le modèle peut légalement émettre 0.
    """
    debut, fin = segment.get("PageDebut"), segment.get("PageFin")
    if debut is None or fin is None:
        return "bornes de pages absentes (PageDebut / PageFin)"
    if isinstance(debut, bool) or isinstance(fin, bool):
        return f"bornes de pages booléennes : {debut!r}-{fin!r}"
    if not isinstance(debut, int) or not isinstance(fin, int):
        return f"bornes de pages non entières : {debut!r}-{fin!r}"
    if debut < 1 or fin < 1:
        return f"bornes < 1 : {debut}-{fin} (numérotation 1-indexée attendue)"
    if debut > fin:
        return f"bornes inversées : {debut} > {fin}"
    if nb_pages and debut > nb_pages:
        return f"page de début {debut} au-delà du document ({nb_pages} pages)"
    return debut, (min(fin, nb_pages) if nb_pages else fin)


def preparer_segments(segments, ocr_response):
    """
    Transforme la liste renvoyée par la segmentation en travaux prêts à extraire.

    Renvoie (travaux, echecs, avertissements). Chaque segment refusé produit un
    échec nommé au lieu de disparaître dans un print(). Un intervalle qui ne
    contient aucune page OCR est refusé AVANT l'appel : le JSON vide partait au
    modèle, qui hallucinait une fiche complète à partir de rien — et on la payait.
    """
    nb_pages = nombre_de_pages(ocr_response)
    travaux, echecs, avertissements = [], [], []

    for index, segment in enumerate(segments):
        titre = segment.get("Titre") or f"Projet {index + 1}"
        verdict = valider_segment(segment, nb_pages)
        if isinstance(verdict, str):
            echecs.append(_echec(index, titre, segment.get("PageDebut"),
                                 segment.get("PageFin"), "segment_invalide",
                                 verdict, False))
            continue
        debut, fin = verdict
        if fin != segment["PageFin"]:
            avertissements.append(
                f"« {titre} » : page de fin {segment['PageFin']} rognée à {fin} "
                f"(le document en compte {nb_pages})"
            )
        contenu = clean_pages(ocr_response, debut, fin)
        if not json.loads(contenu)["pages"]:
            echecs.append(_echec(index, titre, debut, fin, "segment_invalide",
                                 f"aucune page OCR dans l'intervalle {debut}-{fin}",
                                 False))
            continue
        travaux.append({"index": index, "titre": titre, "debut": debut, "fin": fin,
                        "contenu": contenu})

    return travaux, echecs, avertissements


# --- Extraction d'une fiche --------------------------------------------------


def extraire_une_fiche(client, travail, *, prompt_systeme, response_format,
                       prompt_cache_key, modeles_traces, lever_bugs=False):
    """
    Extrait UNE fiche. Exécutée dans un thread du pool.

    Contrat impératif : ne touche à AUCUN `st.*`, et ne lève pas pour un
    problème de données. Elle renvoie (index, fiche | None, echec | None, usage)
    — c'est ce quadruplet qui remplace les trois sites d'engloutissement de la
    version précédente : plus rien ne part vers la sortie du serveur, tout finit
    nommé dans le panneau des échecs, trace comprise.

    `lever_bugs=True` fait remonter les erreurs de catégorie « bug » (nos propres
    AttributeError / TypeError…) au lieu de les convertir. L'appel d'échauffement
    s'en sert : un bug systématique doit avorter le run tout de suite plutôt que
    d'être payé N fois.
    """
    index, titre = travail["index"], travail["titre"]
    debut, fin = travail["debut"], travail["fin"]
    requete = construire_requete_chat(
        prompt_systeme, travail["contenu"],
        modele=MODEL_EXTRACTION,
        response_format=response_format,
        prompt_cache_key=prompt_cache_key,
    )
    try:
        reponse = client.chat.complete(
            **requete, timeout_ms=TIMEOUT_EXTRACTION_MS, retries=RETRY_EXTRACTION
        )
        fiche = json.loads(reponse.choices[0].message.content)
    except Exception as exc:  # converti en échec structuré, pas englouti
        categorie, reessayable, code = classer_erreur(exc)
        if categorie == "bug" and lever_bugs:
            raise
        return index, None, _echec(
            index, titre, debut, fin, categorie, f"{type(exc).__name__}: {exc}",
            reessayable, code, "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)),
        ), usage_vide()

    fiche.update({
        "_project_title": titre,
        "_page_debut": debut,
        "_page_fin": fin,
        "_segment_index": index,
        "_model_ocr": modeles_traces.get("ocr"),
        "_model_segmentation": modeles_traces.get("segmentation"),
        "_model_extraction": _resolved_model(reponse, MODEL_EXTRACTION),
        "_prompt_hash": prompt_cache_key.rsplit("-", 1)[-1],
    })
    return index, fiche, None, usage_depuis_reponse(reponse)


def extraire_fiches(client, travaux, *, prompt_systeme, response_format,
                    prompt_cache_key, modeles_traces, deja_echauffe=False,
                    on_resultat=None, max_workers=MAX_CONCURRENCE_EXTRACTION):
    """
    Extrait les fiches en parallèle, après un appel d'échauffement du cache.

    Ordonnancement imposé par le cache de prompt : si N appels partent
    simultanément, aucun n'a encore écrit le préfixe et **les N manquent**. On
    envoie donc la première fiche seule, puis on éventaille le reste. Le coût de
    l'échauffement est une latence (~10-30 s), le gain est (N-1) × 90 % sur
    ~8 800 jetons de prompt.

    Si l'échauffement échoue sur un problème d'API (429, timeout…), on n'arrête
    PAS le run : l'échec est enregistré (donc réessayable) et le reste part quand
    même — les fiches manqueront le cache, ce qui est une régression de coût, pas
    de correction. En revanche un échec de catégorie « bug » remonte et avorte le
    run : il se reproduirait à l'identique sur les N fiches suivantes.

    `on_resultat(index, fiche, echec, usage)` est appelé sur le thread appelant à
    chaque résultat, dans l'ordre d'achèvement. C'est là que l'appelant persiste
    et met à jour la progression : aucun worker n'écrit, donc aucun verrou.
    """
    fiches, echecs = [], []
    usage_total = usage_vide()

    def encaisser(resultat):
        index, fiche, echec, usage = resultat
        usage_cumuler(usage_total, usage)
        if fiche is not None:
            fiches.append(fiche)
        if echec is not None:
            echecs.append(echec)
        if on_resultat is not None:
            on_resultat(index, fiche, echec, usage)

    restants = list(travaux)
    if restants and not deja_echauffe:
        encaisser(extraire_une_fiche(
            client, restants.pop(0), prompt_systeme=prompt_systeme,
            response_format=response_format, prompt_cache_key=prompt_cache_key,
            modeles_traces=modeles_traces, lever_bugs=True,
        ))

    if restants:
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="rex-extraction") as pool:
            futures = [
                pool.submit(extraire_une_fiche, client, travail,
                            prompt_systeme=prompt_systeme,
                            response_format=response_format,
                            prompt_cache_key=prompt_cache_key,
                            modeles_traces=modeles_traces)
                for travail in restants
            ]
            for future in as_completed(futures):
                # Une exception ici n'est pas une erreur métier (le worker les
                # convertit) : c'est un bug de plomberie, et il doit remonter.
                encaisser(future.result())

    # Ordre document restauré ici, pas par l'ordre d'achèvement. Une liste
    # pré-allouée aurait des trous là où des segments étaient invalides ; le tri
    # rend en plus l'export Excel diffable entre deux runs.
    fiches.sort(key=lambda f: f["_segment_index"])
    echecs.sort(key=lambda e: e["index"])
    return fiches, echecs, usage_total
