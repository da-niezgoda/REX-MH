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
import importlib.metadata
import json
import sqlite3
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

# store et conformite n'importent pas pipeline : le graphe reste acyclique.
# L'orchestration (bas de fichier), déplacée d'app.py en tâche 4, en a besoin ;
# elle n'importe toujours PAS streamlit (voir le docstring du module).
import conformite
import store

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

# En dessous de ce nombre de fiches, on n'échauffe PAS le cache de prompt.
#
# L'échauffement sérialise le premier appel. Avec 2 fiches, cela rend le run
# entièrement séquentiel — mesuré sur l'extrait de 18 pages : 68 s avec
# échauffement contre 32 s sans, pour économiser 90 % des ~11 700 jetons de
# prompt d'UNE seule fiche. Doubler l'attente pour si peu est un mauvais échange ;
# à partir de 3 fiches, le gain croît en (N-1) et l'échauffement redevient payant.
#
# Contrepartie assumée : c'est l'appel d'échauffement qui porte `lever_bugs=True`,
# donc en dessous du seuil un bug de notre code est payé sur 2 appels au lieu
# d'1. À ce volume, c'est négligeable.
SEUIL_ECHAUFFEMENT = 3

# Segmentation par ÉNUMÉRATION + boucle vérifier→raffiner (voir `_segmenter`).
#
# Le petit modèle segmente parfaitement 18 pages (2 fiches, score 1.0) mais se
# « perd au milieu » d'un document long : sur le recueil de 129 pages, l'appel
# unique rendait 10 fiches, propres jusqu'à la p.40 puis un saut direct à 128-129
# (~17 fiches escamotées). Ce n'est PAS un manque de contexte (90 k jetons tiennent
# dans la fenêtre) mais une paresse d'attention. Le fenêtrage glissant, essayé
# avant, relevait le plancher mais plafonnait en rappel (corpus à 24 fiches : 1
# projet perdu) — découper l'espace ne rattrape pas une fiche oubliée DANS une
# fenêtre. On énumère donc sur le document entier, puis une boucle d'auto-critique
# rattrape omissions (via les trous INTÉRIEURS de couverture) et entrées superflues.
#
# Plafond de la boucle de correction ; au-delà, on force le dernier audit assaini
# (filet no-loss). Mesuré : 6/6 corpus « propres », 27 fiches réelles sur le recueil.
MAX_ITER_SEGMENTATION = 3

# Au-delà de cette fraction d'entrées déclarées « superflues », l'audit a confondu
# « à supprimer » avec « vérifié » (observé en direct : un audit rendant les 8
# entrées CORRECTES en superflus, motif « cette entrée est correcte »). On ignore
# alors TOUS les superflus. Retirer à tort perd un projet — la seule issue
# inacceptable ; ajouter à tort ne fait qu'un fantôme. La borne protège ce sens.
SEUIL_SUPERFLUS = 0.5

# Le SDK retombe sur 300 s par opération quand timeout_ms n'est pas passé. Avec
# 4 workers, UN appel pendu immobiliserait 25 % du débit pendant cinq minutes.
# Sur le recueil de 129 pages, une fiche dense (5 pages en entrée, une fiche REX
# complète en sortie) dépasse légitimement 120 s côté medium : d'où 180 s.
TIMEOUT_UPLOAD_MS = 180_000        # 9,4 Mo à téléverser
TIMEOUT_OCR_MS = 600_000           # un recueil de 130 pages met légitimement des minutes
TIMEOUT_SEGMENTATION_MS = 180_000
TIMEOUT_EXTRACTION_MS = 180_000

# Le défaut du SDK est AUCUN retry : un seul 429 perdait une fiche. Le backoff
# intégré honore déjà l'en-tête Retry-After — ne pas écrire de boucle maison.
# Passé par appel et non sur le client : chaque jambe veut un budget différent
# (réessayer quatre fois un téléversement de 9,4 Mo, c'est 38 Mo montés).
#
# INVARIANT : max_elapsed_time DOIT dépasser timeout_ms, sinon un unique délai
# d'attente épuise tout le budget avant le moindre réessai. Le SDK réessaie bien
# les httpx.TimeoutException (dont ReadTimeout) quand retry_connection_errors est
# vrai, mais retry_with_backoff abandonne dès que « écoulé > max_elapsed_time » :
# avec 120 s == 120 s, le premier ReadTimeout n'était JAMAIS réessayé — c'est ce
# qui a perdu une fiche sur le recueil de 129 pages. 300 s laisse la place à un
# réessai après un délai de 180 s (deux tentatives, ~6 min au pire, bornées).
RETRY_EXTRACTION = RetryConfig(
    "backoff",
    BackoffStrategy(initial_interval=1_000, max_interval=20_000, exponent=1.6,
                    max_elapsed_time=300_000),
    True,  # retry_connection_errors : une coupure réseau (ou un ReadTimeout) ne doit pas perdre une fiche
)
RETRY_SEGMENTATION = RetryConfig(
    "backoff",
    BackoffStrategy(initial_interval=1_000, max_interval=20_000, exponent=1.6,
                    max_elapsed_time=300_000),
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


def _trous_interieurs(liste, nb_pages):
    """
    Pages non couvertes SITUÉES ENTRE la première et la dernière page couverte —
    les seuls vrais candidats à un projet omis, offerts en indice au vérificateur.

    On écarte à dessein les pages non couvertes de TÊTE (avant la première fiche)
    et de QUEUE (après la dernière) : ce sont l'introduction, le sommaire et la
    carte en ouverture, les annexes en clôture — que l'énumération exclut à raison.
    Les offrir en indice poussait le vérificateur à les promouvoir en fiches
    fantômes (mesuré sur le recueil réel : intro + sommaire + carte rendus en
    3 fiches). Un trou INTÉRIEUR, lui, est presque toujours une fiche sautée
    (mesuré : les pages 54-75 récupérées sur le même recueil).

    Indice au critique, JAMAIS une donnée assénée : ce n'est pas le cross-check
    `set(1..N) − ∪segments == PagesHorsProjet` que CLAUDE.md interdit (il se
    contredit à grande échelle) — on ne compare rien, on tend des pages à examiner
    et on laisse le modèle trancher page par page.
    """
    couvertes = set()
    for seg in liste or []:
        debut, fin = seg.get("PageDebut"), seg.get("PageFin")
        if isinstance(debut, bool) or isinstance(fin, bool):
            continue
        if not isinstance(debut, int) or not isinstance(fin, int):
            continue
        if debut > fin:
            debut, fin = fin, debut
        couvertes.update(range(max(1, debut), min(nb_pages, fin) + 1))
    if not couvertes:
        return []
    return [p for p in range(min(couvertes), max(couvertes) + 1) if p not in couvertes]


def _audit_propre(critique):
    """Vrai si l'audit ne signale ni omission (`manquants`) ni entrée superflue."""
    if not critique:
        return True
    return not critique.get("manquants") and not critique.get("superflus")


def _ajouter_manquants(liste, critique):
    """
    Filet no-loss de fin de boucle : ajoute les `manquants` du dernier audit comme
    nouveaux segments. **N'applique JAMAIS les `superflus`.**

    Retirer mécaniquement une entrée que l'audit dit superflue perd un vrai projet
    — la seule issue inacceptable. Mesuré sur l'OCR réel de corpus_2 : le
    vérificateur marquait obstinément une fiche RÉELLE (« Petite Camargue ») comme
    superflue à chaque tour ; l'appliquer la supprimait. Les superflus n'agissent
    donc que via la ré-énumération, où le modèle relit les pages et tranche —
    jamais par une suppression aveugle. Ajouter à tort ne fait qu'un fantôme
    (que `preparer_segments` ou l'expert écartent), retirer à tort perd tout :
    le filet ne fait donc qu'ajouter.

    Les `manquants` renvoient à des pages du document ; à n'appeler qu'avec le
    dernier audit, calculé sur la liste courante.
    """
    garde = list(liste or [])
    if not critique:
        return garde
    for m in critique.get("manquants") or []:
        debut, fin = m.get("page_debut"), m.get("page_fin")
        if isinstance(debut, bool) or isinstance(fin, bool):
            continue
        if not isinstance(debut, int) or not isinstance(fin, int):
            continue
        garde.append({
            "PageDebut": debut,
            "PageFin": fin,
            "Titre": m.get("titre") or "Projet",
            "Motif": m.get("motif") or "ajouté d'après l'audit de vérification",
        })
    return garde


def _assainir_critique(critique, nb_candidats):
    """
    Nettoie un audit AVANT tout usage (raffinement ou filet). Ne garde que des
    `superflus` d'index valides, et JETTE tous les superflus si leur nombre
    dépasse `SEUIL_SUPERFLUS × nb_candidats` : au-delà, le modèle a pris
    `superflus` pour un journal de vérification et y a versé des entrées
    correctes (observé en direct). Les `manquants` passent tels quels — ajouter à
    tort ne fait qu'un fantôme, retirer à tort perd un projet. C'est ce garde-fou
    qui interdit à un audit égaré de vider la liste.
    """
    if not critique:
        return critique
    superflus = [c for c in (critique.get("superflus") or [])
                 if isinstance(c.get("index"), int)
                 and not isinstance(c.get("index"), bool)
                 and 0 <= c["index"] < nb_candidats]
    if len(superflus) > SEUIL_SUPERFLUS * nb_candidats:
        superflus = []
    return {"manquants": critique.get("manquants") or [], "superflus": superflus}


def valider_segment(segment, nb_pages):
    """
    Renvoie (debut, fin) en 1-indexé, ou un message d'erreur (str).

    Corrige le `if not start_page` de la version précédente, qui confondait
    PageDebut == 0 avec une clé absente et abandonnait la fiche dans un print()
    vers la sortie du serveur.

    `REXlist.schema.json` impose désormais `minimum: 1`, mais on revérifie ici :
    le mode strict n'est pas une garantie sur laquelle miser une fiche, et cette
    fonction est aussi appelée sur des segments relus depuis
    `runs.segmentation_json`, donc écrits sous une version antérieure du schéma
    — qui admettait 0.
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
    ~8 800 jetons de prompt — donc nul pour N = 1 et faible pour N = 2, d'où
    `SEUIL_ECHAUFFEMENT`.

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
    # Trop peu de fiches pour amortir la sérialisation : voir SEUIL_ECHAUFFEMENT.
    if len(restants) < SEUIL_ECHAUFFEMENT:
        deja_echauffe = True
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


# --- Orchestration : OCR → découpage → extraction → conformité → persistance -
# Ce bloc vivait dans app.py ; déplacé en tâche 4 pour dégager la couche
# Streamlit. Il n'importe toujours PAS streamlit : prompts, schémas, index de
# conformité et registre d'échauffement arrivent par `ctx`, que app.py construit
# sur le thread du script (_contexte_extraction). app.py garde de fines
# enveloppes (parse_pdf_document, relancer, actualiser_lot) qui fabriquent le
# client et le `ctx`, puis délèguent ici.

ORDRE_PHASES = ("upload", "ocr", "segmentation", "extraction")


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
    poids = POIDS_PHASES
    base = sum(v for k, v in poids.items() if ORDRE_PHASES.index(k) < ORDRE_PHASES.index(phase))
    avancement = base + poids[phase] * max(0.0, min(1.0, fraction_phase))
    progress_callback(max(0.0, min(1.0, avancement)), message, phase=phase, **extra)


def version_mistralai():
    try:
        return importlib.metadata.version("mistralai")
    except Exception:
        return None


def _ocr_du_document(client, file_content, filename, *, document_id, cle_ocr,
                     progress_callback=None):
    """
    Charge OCR du document : depuis le cache si possible, sinon appel à l'API.

    Renvoie (charge, depuis_le_cache, model_ocr). La charge est un dict (cache) ou
    une réponse OCR vivante — `_ocr_pages` accepte les deux, donc aucun appelant
    n'a à faire la différence.
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
        timeout_ms=TIMEOUT_UPLOAD_MS,
        retries=RETRY_UPLOAD,
    )
    try:
        signed = client.files.get_signed_url(file_id=televerse.id)
        _avancer(progress_callback, "ocr", 0.1, "Reconnaissance de texte (OCR)…")
        ocr = client.ocr.process(
            model=MODEL_OCR,
            document={"type": "document_url", "document_url": signed.url},
            timeout_ms=TIMEOUT_OCR_MS,
            retries=RETRY_OCR,
            **OCR_PARAMS,
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
        model=_resolved_model(ocr, MODEL_OCR),
        pages_processed=getattr(getattr(ocr, "usage_info", None), "pages_processed", None),
        avg_confidence=confiance_moyenne(ocr),
        sdk_version=version_mistralai(),
    )
    store.set_document_pages(document_id, nombre_de_pages(ocr))
    _avancer(progress_callback, "ocr", 1.0,
             f"OCR terminé — {nombre_de_pages(ocr)} page(s)")
    return ocr, False, _resolved_model(ocr, MODEL_OCR)


def _appel_segmentation(client, contenu, ctx):
    """
    UN appel d'énumération : document entier, ou (aux passages de raffinement)
    document + clé `revision`. Renvoie (segments, model, usage).

    Le prompt système et la clé de cache sont identiques d'un appel à l'autre :
    les passages de raffinement profitent donc du cache de prompt (préfixe système
    commun écrit une fois, puis touché ; seul le message `user` varie).
    """
    requete = construire_requete_chat(
        ctx["prompt_segmentation"],
        contenu,
        modele=MODEL_SEGMENTATION,
        response_format=ctx["format_segmentation"],
        prompt_cache_key=ctx["cle_cache_segmentation"],
    )
    reponse = client.chat.complete(
        **requete,
        timeout_ms=TIMEOUT_SEGMENTATION_MS,
        retries=RETRY_SEGMENTATION,
    )
    try:
        segments = json.loads(reponse.choices[0].message.content).get("Liste", [])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Liste de projets illisible : {exc}") from exc
    return (segments,
            _resolved_model(reponse, MODEL_SEGMENTATION),
            usage_depuis_reponse(reponse))


def _payload_pages(charge_ocr):
    """
    Pages OCR matérialisées une fois en {page_number, content}, réutilisées par
    tous les appels de la boucle (une seule traversée de la charge OCR).
    """
    return [{"page_number": n, "content": c} for n, c in _ocr_pages(charge_ocr)]


def _contenu_enumeration(pages, revision=None):
    """
    Charge `user` de l'énumération : le document, plus — aux passages de
    raffinement — la clé `revision` (liste précédente + audit). Reste un objet
    JSON unique, contrat d'entrée que listPrompt.md décrit.
    """
    obj = {"pages": pages}
    if revision:
        obj["revision"] = revision
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _contenu_verification(pages, liste, pages_non_couvertes):
    """
    Charge `user` de la vérification : le document, la liste candidate INDEXÉE à
    auditer, et les pages que personne ne couvre (indice, pas une vérité).
    """
    liste_a_verifier = [
        {"index": i, "Titre": s.get("Titre") or f"Projet {i + 1}",
         "PageDebut": s.get("PageDebut"), "PageFin": s.get("PageFin")}
        for i, s in enumerate(liste)
    ]
    obj = {"pages": pages, "liste_a_verifier": liste_a_verifier,
           "pages_non_couvertes": pages_non_couvertes}
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _appel_verification(client, contenu, ctx):
    """
    UN appel de vérification (audit) d'une liste candidate. Renvoie
    ({"manquants": [...], "superflus": [...]}, model, usage).

    Prompt système et clé de cache distincts de l'énumération (préfixe
    « verification » ≠ « segmentation »), donc aucune collision de cache.
    """
    requete = construire_requete_chat(
        ctx["prompt_verification"],
        contenu,
        modele=MODEL_SEGMENTATION,
        response_format=ctx["format_verification"],
        prompt_cache_key=ctx["cle_cache_verification"],
    )
    reponse = client.chat.complete(
        **requete,
        timeout_ms=TIMEOUT_SEGMENTATION_MS,
        retries=RETRY_SEGMENTATION,
    )
    try:
        audit = json.loads(reponse.choices[0].message.content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Audit de vérification illisible : {exc}") from exc
    return (
        {"manquants": audit.get("manquants") or [],
         "superflus": audit.get("superflus") or []},
        _resolved_model(reponse, MODEL_SEGMENTATION),
        usage_depuis_reponse(reponse),
    )


def _segmenter(client, charge_ocr, ctx, *, progress_callback=None):
    """
    Découpe le recueil en fiches. Renvoie (segments, model, usage).

    Stratégie (Task 7) : énumérer les projets sur le DOCUMENT ENTIER, puis une
    boucle vérifier→raffiner (≤ MAX_ITER_SEGMENTATION) qui corrige omissions et
    entrées superflues. Le fenêtrage est abandonné : découper l'espace ne rattrape
    pas une fiche oubliée DANS une fenêtre (mesuré) ; l'auto-critique, si. Les
    bornes restées approximatives ici seront affinées par la localisation par REX
    (étape D). Le seam — signature et (segments, model, usage) — ne bouge pas,
    donc traiter_document / relancer / eval_corpus ignorent la boucle.
    """
    pages = _payload_pages(charge_ocr)
    nb_pages = len(pages)
    usage_total = usage_vide()

    _avancer(progress_callback, "segmentation", 0.15, "Énumération des fiches…")
    liste, model, u = _appel_segmentation(client, _contenu_enumeration(pages), ctx)
    usage_cumuler(usage_total, u)

    critique = None
    for i in range(MAX_ITER_SEGMENTATION):
        trous = _trous_interieurs(liste, nb_pages)
        _avancer(progress_callback, "segmentation",
                 0.4 + 0.5 * i / MAX_ITER_SEGMENTATION,
                 f"Vérification du découpage ({len(liste)} fiche(s))…")
        critique, m, u = _appel_verification(
            client, _contenu_verification(pages, liste, trous), ctx)
        usage_cumuler(usage_total, u)
        model = model or m
        critique = _assainir_critique(critique, len(liste))
        if _audit_propre(critique):
            critique = None          # convergé : rien à forcer en fin de boucle
            break
        if i < MAX_ITER_SEGMENTATION - 1:
            revision = {
                "liste_precedente": [
                    {"Titre": s.get("Titre"), "PageDebut": s.get("PageDebut"),
                     "PageFin": s.get("PageFin")} for s in liste],
                "manquants": critique.get("manquants") or [],
                "superflus": critique.get("superflus") or [],
            }
            _avancer(progress_callback, "segmentation",
                     0.4 + 0.5 * (i + 0.5) / MAX_ITER_SEGMENTATION,
                     "Correction de la liste…")
            liste, m, u = _appel_segmentation(
                client, _contenu_enumeration(pages, revision), ctx)
            usage_cumuler(usage_total, u)

    # Boucle épuisée sans converger : filet no-loss — on FORCE l'ajout des
    # manquants du dernier audit (JAMAIS la suppression des superflus : la retirer
    # perdrait un vrai projet — voir _ajouter_manquants). Un superflu résiduel est
    # laissé tel quel : au pire un fantôme, jamais une perte.
    if critique:
        liste = _ajouter_manquants(liste, critique)

    _avancer(progress_callback, "segmentation", 1.0,
             f"{len(liste)} fiche(s) repérée(s)")
    return liste, model or MODEL_SEGMENTATION, usage_total


def resultat_vide(**kw):
    """Forme de retour du pipeline, unique pour les deux modes."""
    resultat = {
        "document_id": None,
        "run_id": None,
        "mode": "rapide",
        "statut": "termine",
        "projects": [],
        "failures": [],
        "usage": usage_neuf(),
        "ocr_cache_hit": False,
        "job_id": None,
        "avertissements": [],
        "filename": None,
    }
    resultat.update(kw)
    return resultat


def traiter_document(client, ctx, file_content, filename, progress_callback=None,
                     mode="rapide"):
    """
    Traite un PDF de bout en bout : OCR, découpage, extraction de chaque fiche.

    Cœur, sans Streamlit : `client` et `ctx` sont fournis par l'enveloppe
    app.parse_pdf_document. Renvoie le dict décrit par resultat_vide.
    """
    sha = sha256_fichier(file_content)
    cle_ocr = cle_cache_ocr(file_content)
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
        return resultat_vide(
            document_id=document_id, mode=mode, statut="echec",
            avertissements=["Ce document est déjà en cours de traitement."],
        )

    usage = usage_neuf()
    resultat = resultat_vide(document_id=document_id, run_id=run_id, mode=mode,
                             usage=usage, filename=filename)
    try:
        charge_ocr, depuis_cache, model_ocr = _ocr_du_document(
            client, file_content, filename, document_id=document_id,
            cle_ocr=cle_ocr, progress_callback=progress_callback,
        )
        resultat["ocr_cache_hit"] = depuis_cache
        usage["ocr"] = {
            "pages_traitees": nombre_de_pages(charge_ocr),
            "octets": len(file_content),
        }

        segments, model_segmentation, usage_seg = _segmenter(
            client, charge_ocr, ctx, progress_callback=progress_callback)
        usage_cumuler(usage["segmentation"], usage_seg)
        usage_cumuler(usage["total"], usage_seg)
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

        travaux, echecs, avertissements = preparer_segments(segments, charge_ocr)
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

        fiches, echecs_extraction, usage_ext, bilan = _extraire_en_parallele(
            client, travaux, ctx=ctx, modeles_traces=modeles_traces,
            document_id=document_id, run_id=run_id,
            progress_callback=progress_callback,
        )
        resultat["projects"] = fiches
        resultat["conformite"] = bilan
        resultat["failures"].extend(echecs_extraction)
        resultat["failures"].sort(key=lambda e: e["index"])
        usage_cumuler(usage["extraction"], usage_ext)
        usage_cumuler(usage["total"], usage_ext)

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


def _conformer_et_persister(run_id, document_id, index, fiche, *, ctx,
                            usage=None, statut="ok"):
    """
    Normalise, valide, persiste. **Point de passage unique des deux modes.**

    Renvoie la fiche normalisée : les appelants doivent l'utiliser à la place de
    celle du modèle, faute de quoi l'écran et l'Excel montreraient une valeur que
    la base ne contient pas.

    La normalisation passe avant la validation, sinon « corrigé » ne serait pas
    exprimable et les erreurs décriraient un état qui n'est pas celui stocké.
    """
    fiche, rapport = conformite.conformer(fiche, ctx["index_conformite"])
    fiche["_validation_status"] = rapport["statut"]
    fiche["_validation_resume"] = conformite.resumer(rapport)
    store.upsert_fiche(
        run_id, document_id, index, status=statut,
        titre=fiche.get("_project_title"),
        page_debut=fiche.get("_page_debut"), page_fin=fiche.get("_page_fin"),
        data=fiche, model_extraction=fiche.get("_model_extraction"),
        prompt_hash=fiche.get("_prompt_hash"), usage=usage,
        validation_status=rapport["statut"],
        # NULL quand il n'y a rien à dire, pour que
        # « WHERE validation_errors_json IS NOT NULL » soit la requête utile.
        validation_errors=rapport if (rapport["erreurs"] or rapport["corrections"]) else None,
    )
    return fiche, rapport


def _bilan_conformite(rapports):
    """Compteurs de conformité d'un run, pour le bandeau de fin."""
    bilan = {statut: 0 for statut in conformite.STATUTS}
    bilan["recalages"] = 0
    bilan["par_regle"] = {}
    for rapport in rapports:
        bilan[rapport["statut"]] = bilan.get(rapport["statut"], 0) + 1
        bilan["recalages"] += conformite.compter_recalages(rapport)
        for correction in rapport.get("corrections") or []:
            regle = correction["regle"]
            bilan["par_regle"][regle] = bilan["par_regle"].get(regle, 0) + 1
    return bilan


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
    echauffees = ctx["cles_echauffees"]
    # Fiches normalisées, indexées par segment. C'est ce qui est renvoyé, à la
    # place de la liste que le pipeline a bâtie : on ne s'appuie PAS sur le fait
    # que `on_resultat` reçoive le même objet que celui empilé dans
    # `extraire_fiches`. Cette identité existe bel et bien, mais c'est exactement
    # le genre de couplage invisible que personne ne retrouve six mois plus tard
    # — deux lignes suffisent à s'en passer.
    normalisees, rapports = {}, []

    def on_resultat(index, fiche, echec, usage_fiche):
        etat["faits"] += 1
        if fiche is not None:
            fiche, rapport = _conformer_et_persister(
                run_id, document_id, index, fiche, ctx=ctx, usage=usage_fiche)
            normalisees[index] = fiche
            rapports.append(rapport)
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
    fiches, echecs, usage_total = extraire_fiches(
        client, travaux,
        prompt_systeme=ctx["prompt_extraction"],
        response_format=ctx["format_extraction"],
        prompt_cache_key=cle_cache,
        modeles_traces=modeles_traces,
        deja_echauffe=cle_cache in echauffees,
        on_resultat=on_resultat,
    )
    echauffees.add(cle_cache)
    if fiches:
        store.set_run_models(run_id, model_extraction=fiches[0]["_model_extraction"])
    # L'ordre document vient du tri de `extraire_fiches`; on ne fait que
    # substituer la version normalisée de chaque fiche.
    fiches = [normalisees.get(f["_segment_index"], f) for f in fiches]
    return fiches, echecs, usage_total, _bilan_conformite(rapports)


def relancer(client, ctx, document_id, run_id, indices, progress_callback=None):
    """
    Ré-extrait une sélection de fiches SANS retoucher à l'OCR : ni téléversement,
    ni ocr.process, ni appel de segmentation. La charge vient du cache et la clé
    de cache de prompt est la même, donc un réessai coûte ce que coûte une fiche
    2+ d'un run normal.

    Garde-fou : si le prompt a changé depuis le run initial, la clé de cache
    change PAR CONSTRUCTION et la fiche relancée sort d'un prompt différent de
    ses sœurs. `_prompt_hash` est stocké par fiche pour que ce mélange soit
    visible au lieu d'être silencieux.

    Cœur sans Streamlit : renvoie `{"erreur": …}` / `{"avertissement": …}` que
    l'enveloppe app.relancer_fiches rend à l'écran, ou le bilan de relance.
    """
    run = store.get_run(run_id)
    payload = store.get_ocr_payload(document_id)
    if run is None or payload is None:
        return {"erreur": "Charge OCR absente du cache : relancez le document complet."}

    charge_ocr = json.loads(payload)
    segments = json.loads(run["segmentation_json"] or '{"Liste": []}').get("Liste", [])
    travaux, _, _ = preparer_segments(segments, charge_ocr)
    retenus = [t for t in travaux if t["index"] in set(indices)]
    if not retenus:
        return {"avertissement": "Aucune fiche relançable dans cette sélection."}

    modeles_traces = {"ocr": run["model_ocr"], "segmentation": run["model_segmentation"]}
    fiches, echecs, _, bilan = _extraire_en_parallele(
        client, retenus, ctx=ctx, modeles_traces=modeles_traces,
        document_id=document_id, run_id=run_id, progress_callback=progress_callback,
    )
    store.finish_run(
        run_id,
        status="termine" if not store.load_failures(run_id) else "partiel",
    )
    return {"relancees": len(fiches), "encore_en_echec": len(echecs),
            "conformite": bilan}


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
        requete = construire_requete_chat(
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
        timeout_ms=TIMEOUT_UPLOAD_MS,
        retries=RETRY_UPLOAD,
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


def actualiser_lot(client, ctx, job_id):
    """
    Sonde un travail par lot et récolte ses résultats s'il est terminé.

    Cœur sans Streamlit : renvoie `{"erreur": …}` si le travail est inconnu (que
    l'enveloppe app.actualiser_travail_par_lot rend à l'écran), sinon un dict de
    compte-rendu.
    """
    enregistre = store.get_batch_job(job_id)
    if enregistre is None:
        return {"erreur": f"Travail {job_id} inconnu de l'historique."}

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

    return _recolter_travail_par_lot(client, travail, enregistre, ctx)


def _recolter_travail_par_lot(client, travail, enregistre, ctx):
    """Rattache chaque ligne de sortie à sa fiche, et nomme les manquantes."""
    run_id, document_id = enregistre["run_id"], enregistre["document_id"]
    correspondance = json.loads(enregistre["fiche_seq_map_json"] or "{}")
    run = store.get_run(run_id)
    rapports = []

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
        # Les colonnes portent la traçabilité que le pipeline injecte en mode
        # rapide : on la reconstitue ici pour que les deux modes stockent la même
        # chose, et pour que le point de passage unique puisse la relire.
        fiche.update({
            "_project_title": reference.get("titre"),
            "_page_debut": reference.get("page_debut"),
            "_page_fin": reference.get("page_fin"),
            "_segment_index": seq,
            "_model_extraction": (run or {}).get("model_extraction") or MODEL_EXTRACTION,
            "_prompt_hash": (run or {}).get("prompt_extraction_sha256", "")[:16],
        })
        _, rapport = _conformer_et_persister(run_id, document_id, seq, fiche, ctx=ctx)
        rapports.append(rapport)

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
            "run_id": run_id, "erreurs_travail": message,
            "conformite": _bilan_conformite(rapports)}
