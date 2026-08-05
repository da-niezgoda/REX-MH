
import functools
import json
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import conformite
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
CLES_PROMPTS = ("REXSchema", "REXListSchema", "REXPrompt", "listPrompt", "vocabulaire")


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


# Le thème vit désormais dans .streamlit/config.toml (tâche 4), atteint tous les
# widgets natifs sans iframe, et remplace la feuille de style injectée. Plus de
# load_css ni de styles.css : une seule source de vérité pour l'apparence.


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


def load_vocabulaire(nom="vocabulary.json"):
    """
    Vocabulaire contrôlé : alias et réglages de normalisation.

    Absent, il n'empêche rien : la canonicalisation résout déjà la casse, les
    accents, les apostrophes et les pluriels sans alias. Un fichier illisible est
    en revanche signalé, parce qu'il changerait silencieusement les recalages.
    """
    if not Path(nom).exists():
        return {}
    try:
        return json.loads(load_text_file(*_cle_fichier(nom)))
    except Exception as e:
        st.error(f"Erreur au chargement du vocabulaire {nom} : {e}")
        return {}


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
    vocabulaire = st.session_state.get("vocabulaire") or {}
    index_conformite, problemes = conformite.construire_index(schema_rex, vocabulaire)
    return {
        "prompt_extraction": prompt_extraction,
        "prompt_segmentation": prompt_segmentation,
        "index_conformite": index_conformite,
        "problemes_vocabulaire": problemes,
        # Le registre d'échauffement reste sous @st.cache_resource ici (singleton
        # de processus, vidable par les tests) ; il voyage dans ctx pour que le
        # pipeline le lise et l'alimente sans importer streamlit.
        "cles_echauffees": _cles_echauffees(),
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


def parse_pdf_document(file_content, filename, progress_callback=None, mode="rapide"):
    """
    Traite un PDF de bout en bout : OCR, découpage, extraction de chaque fiche.

    Enveloppe Streamlit : fabrique le client et le contexte (lecture de
    session_state sur le thread du script), puis délègue à
    pipeline.traiter_document. Le cœur, sans streamlit, vit dans pipeline.py
    depuis la tâche 4. Les échecs par fiche ne sont pas perdus dans un print() :
    ils sont nommés, catégorisés et réessayables.

    Args:
        file_content: contenu binaire du PDF
        filename: nom du fichier (informatif ; l'identité est le hash du contenu)
        progress_callback: appelable (avancement: float, message: str, **détails)
        mode: « rapide » (parallèle, résultat immédiat) ou « economique »
              (API par lot, -50 %, résultat à récolter plus tard)

    Returns:
        dict: voir pipeline.resultat_vide — projects, failures, usage, run_id…
    """
    client = client_mistral()
    if client is None:
        return pipeline.resultat_vide(statut="echec")
    ctx = _contexte_extraction()
    return pipeline.traiter_document(
        client, ctx, file_content, filename,
        progress_callback=progress_callback, mode=mode)


def relancer_fiches(document_id, run_id, indices, progress_callback=None):
    """
    Ré-extrait une sélection de fiches SANS retoucher à l'OCR. Enveloppe
    Streamlit : le cœur est pipeline.relancer, qui renvoie soit un message à
    afficher (`erreur` / `avertissement`), soit le bilan de relance.
    """
    client = client_mistral()
    if client is None:
        return None
    ctx = _contexte_extraction()
    resultat = pipeline.relancer(
        client, ctx, document_id, run_id, indices,
        progress_callback=progress_callback)
    if resultat.get("erreur"):
        st.error(resultat["erreur"])
        return None
    if resultat.get("avertissement"):
        st.warning(resultat["avertissement"])
        return None
    return resultat


def actualiser_travail_par_lot(job_id):
    """
    Sonde un travail par lot et récolte ses résultats s'il est terminé. Enveloppe
    Streamlit : le cœur est pipeline.actualiser_lot.

    Renvoie un dict de compte-rendu, ou None (client indisponible, ou travail
    inconnu de l'historique — message affiché).
    """
    client = client_mistral()
    if client is None:
        return None
    ctx = _contexte_extraction()
    resultat = pipeline.actualiser_lot(client, ctx, job_id)
    if resultat.get("erreur"):
        st.error(resultat["erreur"])
        return None
    return resultat


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
    _afficher_conformite(resultat.get("conformite"))
    _afficher_usage(resultat.get("usage"))


def _afficher_conformite(bilan):
    """
    Tableau de bord de la dérive. Si les recalages augmentent après une édition de
    prompt, cela se voit ici — et non six mois plus tard dans un courriel du
    client.
    """
    if not bilan:
        return
    total = sum(bilan.get(statut, 0) for statut in conformite.STATUTS)
    if not total:
        return
    morceaux = [f"{bilan.get('conforme', 0)} conforme(s)"]
    if bilan.get("corrige"):
        morceaux.append(f"{bilan['corrige']} corrigée(s)")
    if bilan.get("non_conforme"):
        morceaux.append(f"{bilan['non_conforme']} non conforme(s)")
    ligne = " · ".join(morceaux)
    if bilan.get("recalages"):
        ligne += f" — {bilan['recalages']} recalage(s) d'énumération"
    (st.warning if bilan.get("non_conforme") else st.caption)(f"Conformité : {ligne}")

    if bilan.get("par_regle"):
        with st.popover("Détail des recalages", use_container_width=False):
            st.caption(
                "Chaque recalage est une valeur du modèle ramenée à une valeur du "
                "vocabulaire contrôlé. Une hausse signale une dérive du prompt ou "
                "du schéma, pas une amélioration."
            )
            for regle, n in sorted(bilan["par_regle"].items(),
                                   key=lambda kv: -kv[1]):
                st.write(f"- `{regle}` : {n}")


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
    """En-tête de l'application, en éléments natifs (plus de bannière HTML)."""
    st.title("🌿 REX Zones Humides")
    st.caption("Extraction de retours d'expérience depuis un recueil PDF")



SECTION_META = {
    "Presentation": ("📋", "Informations du projet"),
    "Objectif": ("🎯", "Objectif du maître d'ouvrage"),
    "Description": ("📝", "Description"),
    "Enjeux": ("🌱", "Enjeux eau, biodiversité et climat"),
    "Typologie": ("🔧", "Typologie — ingénierie écologique"),
    "Directives": ("🇪🇺", "Référence directives européennes"),
    "Contexte": ("⚖️", "Contexte réglementaire"),
    "Valorisation": ("🏆", "Valorisation de l'opération"),
    "Travaux": ("🗺️", "Période et envergure des travaux"),
    "Documents": ("📚", "Documents"),
}

# Libellés d'affichage, par « Section/champ ». La STRUCTURE et l'ORDRE viennent
# du schéma (SECTIONS + REX.schema.json) ; seuls les libellés humains vivent ici,
# HORS du schéma — les y mettre alourdirait le prompt et déplacerait
# prompt_cache_key. Un champ absent retombe sur une version « jolie » de sa clé,
# si bien qu'un nouveau champ du schéma s'affiche sans toucher au moteur de rendu.
LIBELLES = {
    "Presentation/Titre": "Titre",
    "Presentation/Bassin": "Bassin",
    "Presentation/Nom de l'organisme": "Nom de l'organisme",
    "Presentation/Localisation": "Localisation",
    "Presentation/Adresse précise": "Adresse précise",
    "Presentation/Région": "Région",
    "Objectif/objectifs": "Objectifs",
    "Description/resume": "Résumé",
    "Description/publication_recueil": "Publication",
    "Enjeux/date_debut": "Date de début",
    "Enjeux/date_fin": "Date de fin",
    "Enjeux/enjeux": "Enjeux",
    "Contexte/contexte": "Contexte",
    "Contexte/autres": "Autres",
    "Travaux/surface_travaux": "Surface des travaux",
    "Documents/pages_extraire": "Pages à extraire",
    "Documents/recueil_complet": "Recueil complet",
}

# Champs porteurs d'une URL : rendus en lien natif s'ils sont http(s).
CHAMPS_URL = {"Valorisation/url", "Documents/recueil_complet"}

# Verdict de conformité → libellé pour la colonne du tableau de résultats.
_VERDICT_LIBELLE = {
    "conforme": "✅ Conforme",
    "corrige": "✏️ Corrigé",
    "non_conforme": "⚠️ Non conforme",
}


def _libelle_verdict(statut):
    return _VERDICT_LIBELLE.get(statut, "—")


def _libelle_champ(section, champ):
    """Libellé humain d'un champ : LIBELLES, sinon la clé « jolie » (repli)."""
    return LIBELLES.get(f"{section}/{champ}") or champ.replace("_", " ").capitalize()


def _valeur_affichable(valeur):
    """
    Texte affichable d'une valeur de champ.

    Les listes sont jointes par « , » — uniqueItems ayant été retiré du schéma,
    un tableau peut apparaître dans n'importe quelle section. Aucun échappement
    ici : le rendu natif (st.markdown sans unsafe_allow_html) n'exécute pas le
    HTML, donc un « <script> » venu de l'OCR ou d'une archive s'affiche en texte.
    C'est ce qui remplace les anciens _e()/_url().
    """
    if valeur is None:
        return ""
    if isinstance(valeur, (list, tuple)):
        return ", ".join(str(v) for v in valeur if v not in (None, ""))
    return str(valeur)


def _est_http(texte):
    """Vrai si le texte est une URL http(s) — les seules rendues en lien."""
    return str(texte or "").strip().lower().startswith(("http://", "https://"))


def blocs_de_fiche(project):
    """
    Structure d'affichage d'une fiche, PURE et testable.

    Renvoie une liste de blocs (un par section non vide), chacun sous la forme
    (emoji, titre, [(libellé, texte, est_lien), …]). La structure et l'ordre
    viennent du schéma (SECTIONS) ; les libellés de LIBELLES avec repli sur la
    clé. Aucun st.* ici — `rendre_fiche` ne fait qu'émettre ce que renvoie cette
    fonction, ce qui la rend vérifiable sans Streamlit.
    """
    blocs = []
    for section in SECTIONS:
        donnees = project.get(section) if project else None
        if not isinstance(donnees, dict) or not any(donnees.values()):
            continue
        emoji, titre = SECTION_META.get(section, ("", section))
        champs = []
        for champ, valeur in donnees.items():
            texte = _valeur_affichable(valeur)
            if not texte:
                continue
            if section == "Description" and champ == "resume" and len(texte) > 500:
                texte = texte[:500] + "…"
            chemin = f"{section}/{champ}"
            est_lien = chemin in CHAMPS_URL and _est_http(texte)
            champs.append((_libelle_champ(section, champ), texte, est_lien))
        if champs:
            blocs.append((emoji, titre, champs))
    return blocs


def rendre_fiche(project):
    """
    Rend le détail d'une fiche en éléments natifs Streamlit (une carte par
    section). Remplace l'ancien format_expanded_data et son HTML brut : plus de
    _e()/_url() ni de unsafe_allow_html, l'échappement est celui, natif, de
    st.markdown.
    """
    blocs = blocs_de_fiche(project)
    if not blocs:
        st.info("Aucune donnée disponible pour cette fiche.")
        return
    for emoji, titre, champs in blocs:
        with st.container(border=True):
            st.markdown(f"##### {emoji} {titre}".strip())
            for libelle, texte, est_lien in champs:
                if est_lien:
                    st.markdown(f"**{libelle} :** [{texte}]({texte})")
                else:
                    st.markdown(f"**{libelle} :** {texte}")


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
# Le verdict de conformité ferme la liste : c'est ce qui permet au client de
# trier ou filtrer les lignes à reprendre directement dans le classeur.
META_EXPORT = (
    '_project_title', '_page_debut', '_page_fin', '_segment_index',
    '_model_ocr', '_model_segmentation', '_model_extraction', '_prompt_hash',
    '_validation_status', '_validation_resume',
)


def noms_de_feuilles(schema):
    """Noms de champs feuille du schéma, section par section."""
    noms = []
    for section, noeud in (schema.get("properties") or {}).items():
        for champ in (noeud.get("properties") or {}):
            noms.append((section, champ))
    return noms


def verifier_unicite_des_feuilles(schema):
    """
    Noms de feuille apparaissant dans plus d'une section.

    `flatten_project_data` aplatit sur le nom de feuille NU, sans préfixe de
    section : deux sections partageant un nom de champ s'écraseraient donc en
    silence, et une colonne disparaîtrait de l'Excel du client. On ne change pas
    le schéma d'aplatissement — les noms nus sont le contrat de colonnes du
    client — on verrouille l'invariant par un test, pour que l'ajout d'un
    doublon en tâche 5 fasse échouer la suite au lieu de passer inaperçu.
    """
    vus, collisions = {}, {}
    for section, champ in noms_de_feuilles(schema):
        if champ in vus:
            collisions.setdefault(champ, [vus[champ]]).append(section)
        else:
            vus[champ] = section
    return collisions


def flatten_project_data(project):
    """
    Aplatit une fiche en un dict à un seul niveau pour l'export Excel.

    Les listes sont jointes pour TOUTES les sections. La version précédente ne
    le faisait que dans la branche Enjeux, si bien qu'un tableau venu d'une autre
    section (type_valorisation, notamment) arrivait brut jusqu'à xlsxwriter et
    ressortait en « ['Document de communications'] », crochets compris.

    Limite connue et verrouillée : les clés sont aplaties à leur nom feuille, sans
    préfixe de section. Voir `verifier_unicite_des_feuilles`.
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
    # Rechargé depuis la base, le verdict arrive en JSON brut : le résumé
    # français est dérivé ici, pour que `store.py` reste un module feuille.
    if '_validation_resume' not in flat_data and project.get('_validation_errors_json'):
        flat_data['_validation_resume'] = conformite.resumer_json(
            project['_validation_errors_json'])

    return flat_data


def create_excel_download(projects):
    """Classeur Excel des fiches, en octets."""
    df = pd.DataFrame([flatten_project_data(project) for project in projects])
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='REX')
    return output.getvalue()


def _titre_de_fiche(project, index):
    """
    Libellé d'une fiche : la trace du pipeline d'abord, le titre extrait ensuite,
    un rang en dernier recours.

    Extrait d'une branche `except` du tableau, où il était intestable sans
    Streamlit. Le bug d'origine lisait « presentation » / « titre » en minuscules
    alors que le schéma produit « Presentation » / « Titre » : la recherche
    échouait toujours et chaque ligne s'appelait « Projet N », alors que
    `_project_title` était disponible. Les clés minuscules ne sont volontairement
    PAS acceptées — elles n'ont jamais existé dans le schéma.
    """
    return (
        project.get("_project_title")
        or (project.get("Presentation") or {}).get("Titre")
        or f"Projet {index + 1}"
    )


def _nom_export(filename):
    """Nom de fichier d'export, sans l'extension d'origine.

    `.replace('.pdf', '')` était sensible à la casse : IFD_….PDF donnait
    « IFD_….PDF_REX_export.xlsx ».
    """
    base = filename or "export"
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return f"{base}_REX_export.xlsx"


@st.fragment
def display_results_table():
    """
    Tableau natif des fiches extraites ; sélectionner une ligne affiche son
    détail dessous (rendu natif, schéma → LIBELLES). Fragment : la sélection
    ne rejoue que ce bloc, pas toute la page. Remplace st_mui_table (iframe,
    non maintenu) et sa CSS dupliquée.
    """
    if 'last_parsed_data' not in st.session_state:
        return

    data = st.session_state.last_parsed_data
    projects = data.get('projects', [])
    if not projects:
        return

    st.divider()
    st.subheader(f"📊 Résultats — {data['filename']}")
    st.caption(f"{len(projects)} projet(s) extrait(s) · {data['date']}")

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

    lignes = [{
        "Titre du projet": _titre_de_fiche(project, idx),
        "Page début": project.get("_page_debut"),
        "Page fin": project.get("_page_fin"),
        "Conformité": _libelle_verdict(project.get("_validation_status")),
    } for idx, project in enumerate(projects)]

    etat = st.dataframe(
        pd.DataFrame(lignes),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        # Sans key stable, la sélection serait perdue à chaque rerun.
        key="tableau_resultats",
    )

    # Accès par item (dict) : la valeur de retour de st.dataframe est un dict.
    rows = etat["selection"]["rows"] if etat and "selection" in etat else []
    idx = rows[0] if rows else 0
    st.markdown(f"#### 📄 {_titre_de_fiche(projects[idx], idx)}")
    rendre_fiche(projects[idx])


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
                               mistralai_version=pipeline.version_mistralai())


def _display_maintenance():
    """Rechargement des prompts et des schémas depuis le disque."""
    with st.popover("⚙️ Maintenance"):
        st.caption(
            "Recharge les prompts Markdown, les schémas JSON et le vocabulaire "
            "depuis le disque. Nécessaire après avoir édité REXPrompt.md, "
            "listPrompt.md, REX.schema.json, REXlist.schema.json ou "
            "vocabulary.json : ils sont chargés une fois par session, donc un "
            "simple rerun ne suffit pas."
        )
        if st.button("♻️ Recharger prompts et schémas", key="recharger_prompts"):
            for cle in CLES_PROMPTS:
                st.session_state.pop(cle, None)
            load_text_file.clear()
            st.toast("Prompts, schémas et vocabulaire rechargés")
            st.rerun()

        # Un alias qui vise une valeur absente de l'énumération n'est pas une
        # erreur fatale — « Site Natura 2000 » n'existe pas encore dans
        # Contexte.contexte, et c'est la tâche 5 qui l'ajoutera. Mais il ne doit
        # pas rester invisible, sinon un alias mal orthographié ne se recale
        # jamais sans que personne ne le sache.
        problemes = conformite.construire_index(
            st.session_state.REXSchema, st.session_state.get("vocabulaire") or {})[1]
        if problemes:
            st.warning("Vocabulaire — " + str(len(problemes)) + " point(s) à revoir :")
            for probleme in problemes:
                st.caption(f"• {probleme}")

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

    # Absent ou vide, le vocabulaire n'empêche rien : la canonicalisation résout
    # déjà la casse, les accents, les apostrophes et les pluriels sans alias. Pas
    # de st.stop() donc.
    if 'vocabulaire' not in st.session_state:
        st.session_state.vocabulaire = load_vocabulaire()


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


