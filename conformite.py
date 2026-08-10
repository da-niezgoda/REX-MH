"""
Normalisation et validation d'une fiche REX, après génération.

Une seule passe, un seul objet de sortie, un seul index dérivé du schéma — d'où
un seul module là où le plan en prévoyait deux : validation et normalisation ont
besoin des mêmes 12 emplacements d'énumération, des mêmes 6 champs à motif et du
même `Draft7Validator`. Les séparer imposerait de parcourir le schéma deux fois.

**N'importe pas streamlit**, comme `pipeline.py` et `store.py` : voir le CLAUDE.md
du projet. Toutes les fonctions sont pures — `conformer` renvoie un nouveau dict
et ne touche jamais son argument.

## Le principe de sûreté

On ne recale une valeur que si elle est identique à une valeur de l'énumération
*après canonicalisation*, ou si un alias explicite la désigne. **Jamais de
distance d'édition.** La raison n'est pas la prudence mais la démontrabilité :
chaque index est vérifié INJECTIF sur le schéma courant (voir
`verifier_injectivite`, appelée par les tests), donc un recalage est une *preuve*
que la sortie du modèle et la valeur de l'énumération ne diffèrent que d'une façon
invariante par normalisation. Une distance d'édition n'offre rien de tel : avec
« Fauche », « Fenaison et pâture » et « Pâturage » dans une même énumération de
53 valeurs, elle corromprait des données expertes sans qu'on puisse dire
lesquelles.

Une valeur non résolue est **laissée telle quelle** et signalée. La couche ne
devine pas.
"""
import copy
import hashlib
import json
import re
import unicodedata

import jsonschema

STATUTS = ("conforme", "corrige", "non_conforme")
VERSION_RAPPORT = 1

# Apostrophes typographiques et droites : les énumérations métier en manquent
# beaucoup (« Conservation despèces patrimoniales ») mais pas toutes
# (« 1 - Étangs d'aquaculture »). Les supprimer des deux côtés rend la
# comparaison indifférente à cette incohérence.
_APOSTROPHES = "'’ʼ´`ʻ"

# Espaces que l'OCR et les copier-coller sèment : insécable, insécable étroite,
# fine, chasse nulle. `str.split()` ne les traite pas tous comme séparateurs.
_ESPACES = "   ​  "

# Trait d'union conditionnel : invisible, et il casse toute comparaison.
_INVISIBLES = "­﻿"

_CODE_LIBELLE = re.compile(r"^([0-9A-Za-z()]{1,6}) - (.+)$")

# Dernier groupe de 4 chiffres d'une chaîne : l'année d'une date « JJ/MM/AAAA »,
# « MM/AAAA » ou déjà « AAAA ». Sert à la règle de format `annee` (tâche 5).
_MOTIF_ANNEE = re.compile(r"(\d{4})\s*$")

# Ordre des tentatives. Le premier tier qui répond gagne, et son nom est
# journalisé : c'est ce qui rend chaque recalage attribuable.
TIERS = ("espaces", "canonique", "alias", "code", "libelle", "pluriel",
         "libelle_pluriel")

_MESSAGES = {
    "enum": "valeur hors énumération contrôlée",
    "pattern": "format attendu non respecté",
    "required": "champ obligatoire absent",
    "type": "type de donnée incorrect",
    "additionalProperties": "champ inconnu pour cette section",
    "maxLength": "valeur trop longue",
    "minItems": "liste trop courte",
}


def empreinte(texte):
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


# --- Canonicalisation --------------------------------------------------------


def nettoyer_espaces(valeur, *, ligne_unique=True):
    """
    Espaces normalisés, sans rien changer d'autre.

    `ligne_unique` décide de l'agressivité, et la distinction est essentielle :

    · **True** — pour une valeur d'énumération ou un champ à `pattern` : ce sont
      des jetons d'une seule ligne, donc écraser toutes les suites d'espaces est
      exactement ce qu'il faut (« 2006 » entouré d'espaces devient « 2006 »).

    · **False** — pour du texte libre. On se contente d'ôter les espaces de bord
      et de ramener les espaces exotiques à l'espace ordinaire. **Surtout, on ne
      touche pas aux retours à la ligne** : `Description.resume` est un récit de
      plusieurs paragraphes séparés par « \\n\\n », et les écraser détruisait la
      mise en forme d'un texte que le client lit — tout en rendant chaque fiche
      « corrigée », ce qui mettait la barre « zéro recalage = propre »
      définitivement hors d'atteinte. Constaté sur un run réel, pas en théorie.
    """
    texte = str(valeur)
    for c in _INVISIBLES:
        texte = texte.replace(c, "")
    for c in _ESPACES:
        texte = texte.replace(c, " ")
    return " ".join(texte.split()) if ligne_unique else texte.strip()


def canoniser(valeur):
    """
    Clé de comparaison insensible à la casse, aux accents, aux apostrophes et à
    la ponctuation.

    **L'ordre compte** : les apostrophes doivent disparaître AVANT que la
    ponctuation ne devienne des espaces, sans quoi « d'espèces » donnerait
    « d especes » et ne rejoindrait pas « despeces ».

    Vérifié injectif sur les 12 emplacements d'énumération du schéma courant.
    """
    texte = unicodedata.normalize("NFKD", str(valeur or ""))
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    texte = texte.casefold()
    texte = "".join(c for c in texte if c not in _APOSTROPHES)
    texte = re.sub(r"[^0-9a-z]+", " ", texte)
    return " ".join(texte.split())


def depluraliser(cle_canonique):
    """
    Pluriel naïf retiré, mot à mot. Seule règle morphologique admise, et le
    dernier tier : elle est déterministe, idempotente, et son injectivité est
    revérifiée à chaque exécution des tests.
    """
    return " ".join(m[:-1] if len(m) > 3 and m.endswith("s") else m
                    for m in cle_canonique.split())


def _reduire_annee(valeur):
    """
    Année (AAAA) extraite d'une date « JJ/MM/AAAA », « MM/AAAA » ou déjà « AAAA ».

    Renvoie la valeur inchangée si aucun groupe de 4 chiffres ne s'y trouve — donc
    « » reste « ». Pilotée par `regles` dans vocabulary.json, PAS par le nom du
    champ : c'est la première règle de format, ajoutée en tâche 5 pour réduire les
    dates que le modèle émettait en « 01/01/AAAA » (jour et mois fabriqués, faute
    de pouvoir laisser vide un motif requis JJ/MM/AAAA). Le nouveau motif du schéma
    `^(\\d{4})?$` fait que le modèle émet désormais directement l'année ; cette
    réduction ne mord donc que sur une archive relue à l'ancien format.
    """
    correspondance = _MOTIF_ANNEE.search(valeur)
    return correspondance.group(1) if correspondance else valeur


# --- Index dérivé du schéma --------------------------------------------------


def _feuilles(schema, prefixe=""):
    """(chemin « Section/champ », nœud, est_tableau) pour chaque feuille."""
    for nom, noeud in (schema.get("properties") or {}).items():
        chemin = f"{prefixe}{nom}"
        if noeud.get("type") == "object":
            yield from _feuilles(noeud, prefixe=f"{chemin}/")
        elif noeud.get("type") == "array":
            yield chemin, noeud.get("items") or {}, True
        else:
            yield chemin, noeud, False


def construire_index(schema, vocabulaire=None):
    """
    Index de résolution, plus la liste des problèmes rencontrés en le bâtissant.

    Les problèmes (un alias qui vise une valeur inexistante, une cible de routage
    inconnue, une clé d'export hors énumération) sont RENVOYÉS et non levés : ils
    ne doivent pas empêcher l'application de démarrer, et le popover Maintenance
    les affiche. La tâche 5 a comblé le manque historique (« Site Natura 2000 »
    absent de Contexte/contexte) ; l'ensemble attendu est donc désormais vide.
    """
    vocabulaire = vocabulaire or {}
    alias_vocab = vocabulaire.get("alias") or {}
    tiers_off = vocabulaire.get("tiers_desactives") or {}
    regles = vocabulaire.get("regles") or {}
    problemes = []

    champs = {}
    for chemin, noeud, est_tableau in _feuilles(schema):
        enum = noeud.get("enum")
        entree = {
            "est_tableau": est_tableau,
            "type": noeud.get("type"),
            "motif": noeud.get("pattern"),
            "enum": enum,
            "tiers_desactives": set(tiers_off.get(chemin) or ()),
            "format": (regles.get(chemin) or {}).get("format"),
        }
        if enum:
            entree["exact"] = set(enum)
            entree["canonique"] = {canoniser(v): v for v in enum if v}
            entree["pluriel"] = {depluraliser(canoniser(v)): v for v in enum if v}
            nonvides = [v for v in enum if v]
            if nonvides and all(_CODE_LIBELLE.match(v) for v in nonvides):
                # Détecté sur la forme, pas sur le nom du champ : si la tâche 5
                # ajoute un autre vocabulaire codé, il en bénéficie sans code.
                entree["code"] = {}
                entree["libelle"] = {}
                # Le libellé AU SINGULIER, sans son code. Index distinct de
                # `pluriel`, qui est bâti sur la valeur complète et contient donc
                # le code : « Tourbière non boisée » ne pouvait pas l'atteindre.
                entree["libelle_pluriel"] = {}
                for v in nonvides:
                    code, libelle = _CODE_LIBELLE.match(v).groups()
                    entree["code"][canoniser(code)] = v
                    entree["libelle"][canoniser(libelle)] = v
                    entree["libelle_pluriel"][depluraliser(canoniser(libelle))] = v
            aliases = {}
            for brut, cible in (alias_vocab.get(chemin) or {}).items():
                if cible not in entree["exact"]:
                    problemes.append(
                        f"{chemin} : l'alias « {brut} » vise « {cible} », "
                        f"qui n'est pas dans l'énumération")
                    continue
                aliases[canoniser(brut)] = cible
            entree["alias"] = aliases
        champs[chemin] = entree

    for chemin in alias_vocab:
        if chemin not in champs:
            problemes.append(f"alias déclarés pour « {chemin} », champ inconnu du schéma")
        elif not champs[chemin].get("enum"):
            problemes.append(f"alias déclarés pour « {chemin} », qui n'est pas une énumération")

    # Routage : une valeur d'énumération retirée est redirigée vers un champ libre
    # plutôt que perdue (tâche 5 : Contexte/contexte → Contexte/autres). La source
    # doit être une énumération, la cible un champ existant.
    routage = {}
    for source, cible in (vocabulaire.get("routage") or {}).items():
        if source not in champs or not champs[source].get("enum"):
            problemes.append(f"routage : « {source} » n'est pas une énumération du schéma")
        elif cible not in champs:
            problemes.append(f"routage : la cible « {cible} » est inconnue du schéma")
        else:
            routage[source] = cible

    # Clés d'export (libellé → clé de base), pour l'EXPORT seulement, jamais le
    # stockage. Attachées à l'entrée UNIQUEMENT si la table est COMPLÈTE — toutes
    # les valeurs non vides ont une clé. Ce garde interdit un export à moitié
    # traduit tant que le client n'a pas fourni les clés manquantes (tâche 5).
    for chemin, table in (vocabulaire.get("cles_export") or {}).items():
        entree = champs.get(chemin)
        if entree is None or not entree.get("enum"):
            problemes.append(f"clés d'export pour « {chemin} », qui n'est pas une énumération")
            continue
        inconnues = [k for k in table if k not in entree["exact"]]
        if inconnues:
            problemes.append(
                f"clés d'export pour « {chemin} » : valeur(s) hors énumération : {inconnues}")
            continue
        if all(v in table for v in entree["enum"] if v):
            entree["cles_export"] = dict(table)

    index = {
        "validateur": jsonschema.Draft7Validator(schema),
        "champs": champs,
        "routage": routage,
        "schema_sha256": empreinte(json.dumps(schema, sort_keys=True, ensure_ascii=False)),
        "vocabulaire_sha256": empreinte(
            json.dumps(vocabulaire, sort_keys=True, ensure_ascii=False)),
    }
    return index, problemes


def verifier_injectivite(index):
    """
    Les collisions de chaque tier, sur le schéma courant. Vide = tout va bien.

    Appelée par les tests plutôt qu'au démarrage : c'est un invariant du
    vocabulaire, et c'est la tâche 5 qui risque de le rompre en réécrivant les
    énumérations. Le test échoue alors, et le correctif est une ligne de
    `tiers_desactives` dans `vocabulary.json`.
    """
    collisions = []
    for chemin, entree in index["champs"].items():
        if not entree.get("enum"):
            continue
        for tier in ("canonique", "pluriel", "code", "libelle", "libelle_pluriel"):
            table = entree.get(tier)
            if not table:
                continue
            vus = {}
            for valeur in entree["enum"]:
                if not valeur:
                    continue
                cle = _cle_du_tier(tier, valeur, entree)
                if cle is None:
                    continue
                if cle in vus and vus[cle] != valeur:
                    collisions.append(f"{chemin} [{tier}] : « {vus[cle]} » et "
                                      f"« {valeur} » partagent la clé « {cle} »")
                vus[cle] = valeur
    return collisions


def _cle_du_tier(tier, valeur, entree):
    """Clé qu'une valeur d'énumération occupe dans la table d'un tier."""
    if tier == "canonique":
        return canoniser(valeur)
    if tier == "pluriel":
        return depluraliser(canoniser(valeur))
    correspondance = _CODE_LIBELLE.match(valeur)
    if not correspondance:
        return None
    code, libelle = correspondance.groups()
    if tier == "code":
        return canoniser(code)
    if tier == "libelle":
        return canoniser(libelle)
    return depluraliser(canoniser(libelle))


# --- Résolution d'une valeur ------------------------------------------------


def resoudre(valeur, entree):
    """
    (valeur retenue, règle appliquée). `regle` vaut None si rien n'a changé.

    Renvoie la valeur d'origine si aucun tier ne répond : ne jamais deviner.
    """
    if not isinstance(valeur, str):
        return valeur, None

    off = entree["tiers_desactives"]
    enum = entree.get("enum")

    # Une énumération ou un motif décrit un jeton d'une seule ligne ; tout le
    # reste est du texte libre, dont les retours à la ligne portent du sens.
    ligne_unique = bool(enum) or bool(entree.get("motif"))
    propre = nettoyer_espaces(valeur, ligne_unique=ligne_unique)
    if enum is None:
        # Champ à règle de format (ex. date → année) : appliquée sur la valeur
        # déjà nettoyée de ses espaces, en un seul recalage nommé.
        if entree.get("format") == "annee":
            reduit = _reduire_annee(propre)
            return (reduit, "format_annee") if reduit != valeur else (valeur, None)
        # Champ libre ou à motif : les espaces, et rien de plus.
        return (propre, "espaces") if propre != valeur else (valeur, None)

    if valeur in entree["exact"]:
        return valeur, None
    if "espaces" not in off and propre in entree["exact"]:
        return propre, "espaces"
    if not propre:
        # Le vide n'est pas résoluble : ou il est dans l'énumération (traité
        # ci-dessus), ou c'est une absence de valeur qu'il faut signaler.
        return (propre, "espaces") if propre != valeur else (valeur, None)

    cle = canoniser(propre)
    depluralisee = depluraliser(cle)
    noms = {"code": "code_ramsar", "libelle": "libelle_ramsar",
            "libelle_pluriel": "libelle_ramsar_pluriel"}
    for tier in ("canonique", "alias", "code", "libelle", "pluriel", "libelle_pluriel"):
        if tier in off:
            continue
        table = entree.get(tier)
        if not table:
            continue
        candidate = table.get(
            depluralisee if tier in ("pluriel", "libelle_pluriel") else cle)
        if candidate is not None and candidate != valeur:
            return candidate, noms.get(tier, tier)
    return valeur, None


def _resoudre_liste(valeurs, entree):
    """
    Liste normalisée, plus les corrections. Vide et doublons retirés.

    `uniqueItems` a dû être retiré du schéma pour le mode strict de Mistral, donc
    un tableau peut légalement contenir des doublons : c'est ici qu'on les ôte.
    """
    retenues, vues, corrections = [], set(), []
    for position, brute in enumerate(valeurs):
        valeur, regle = resoudre(brute, entree)
        if regle:
            corrections.append((position, brute, valeur, regle))
        if isinstance(valeur, str) and not valeur.strip():
            corrections.append((position, brute, None, "vide_supprime"))
            continue
        cle = canoniser(valeur) if isinstance(valeur, str) else repr(valeur)
        if cle in vues:
            corrections.append((position, brute, None, "doublon_supprime"))
            continue
        vues.add(cle)
        retenues.append(valeur)
    return retenues, corrections


def appliquer_cles_export(entree, valeur):
    """
    Valeur(s) remplacée(s) par leur clé de base, si le champ a une table de clés
    COMPLÈTE ; sinon rendue(s) telle(s) quelle(s).

    Le garde de complétude (posé dans `construire_index`) interdit un export à
    moitié traduit : tant que toutes les valeurs d'énumération n'ont pas de clé,
    `entree["cles_export"]` reste absent et l'export garde les libellés. Appelée
    par la couche d'export d'`app.py`, jamais au stockage. Tâche 5 : mécanisme
    livré, clés manquantes différées (une simple édition de `vocabulary.json`).
    """
    table = (entree or {}).get("cles_export")
    if not table:
        return valeur
    if isinstance(valeur, list):
        return [table.get(v, v) if isinstance(v, str) else v for v in valeur]
    if isinstance(valeur, str):
        return table.get(valeur, valeur)
    return valeur


def _router_valeurs(fiche, index):
    """
    Déplace vers un champ libre toute valeur d'énumération non admise pour
    laquelle un routage est déclaré (tâche 5 : `Contexte/contexte` → `.../autres`).

    N'intervient JAMAIS sur une extraction fraîche — le mode strict n'émet que des
    valeurs admises ou vides. C'est le filet pour une archive relue sous un schéma
    dont l'énumération a rétréci : le statut retiré est *redirigé*, pas perdu, et
    la fiche redevient conforme. Journalise `avant`/`apres` sur les DEUX champs,
    donc reste réversible ; et idempotent, une source vidée n'étant plus routée.
    """
    corrections = []
    for source, cible in index.get("routage", {}).items():
        sec_s, ch_s = source.split("/", 1)
        contenu_s = fiche.get(sec_s)
        if not isinstance(contenu_s, dict) or not isinstance(contenu_s.get(ch_s), str):
            continue
        brute = contenu_s[ch_s]
        propre = nettoyer_espaces(brute, ligne_unique=True)
        if not propre or propre in index["champs"][source]["exact"]:
            continue
        sec_c, ch_c = cible.split("/", 1)
        contenu_c = fiche.setdefault(sec_c, {})
        existant = contenu_c.get(ch_c)
        fusion = f"{existant} ; {propre}" if (existant or "").strip() else propre
        corrections.append({"chemin": cible, "avant": existant,
                            "apres": fusion, "regle": "route_vers_autres"})
        contenu_c[ch_c] = fusion
        corrections.append({"chemin": source, "avant": brute,
                            "apres": "", "regle": "route_vers_autres"})
        contenu_s[ch_s] = ""
    return corrections


# --- Passe complète ----------------------------------------------------------


def conformer(fiche, index):
    """
    (fiche normalisée, rapport). Pure : `fiche` n'est jamais modifiée.

    La normalisation passe AVANT la validation, sans quoi « corrigé » ne serait
    pas exprimable et les erreurs décriraient un état qui n'est pas celui stocké.
    """
    fiche = copy.deepcopy(fiche)
    techniques = {c: fiche.pop(c) for c in list(fiche) if c.startswith("_")}
    corrections = []

    for chemin, entree in index["champs"].items():
        section, champ = chemin.split("/", 1)
        contenu = fiche.get(section)
        if not isinstance(contenu, dict) or champ not in contenu:
            continue
        valeur = contenu[champ]

        if entree["est_tableau"]:
            if not isinstance(valeur, list):
                continue
            retenues, journal = _resoudre_liste(valeur, entree)
            if journal:
                contenu[champ] = retenues
                for position, avant, apres, regle in journal:
                    corrections.append({"chemin": f"{chemin}[{position}]",
                                        "avant": avant, "apres": apres, "regle": regle})
            continue

        nouvelle, regle = resoudre(valeur, entree)
        if regle:
            contenu[champ] = nouvelle
            corrections.append({"chemin": chemin, "avant": valeur,
                                "apres": nouvelle, "regle": regle})

    corrections += _router_valeurs(fiche, index)

    erreurs = [_erreur(e) for e in sorted(
        index["validateur"].iter_errors(fiche), key=lambda e: list(e.path))]

    statut = ("non_conforme" if erreurs
              else "corrige" if corrections
              else "conforme")
    rapport = {
        "version": VERSION_RAPPORT,
        "statut": statut,
        "schema_sha256": index["schema_sha256"],
        "vocabulaire_sha256": index["vocabulaire_sha256"],
        "corrections": corrections,
        "erreurs": erreurs,
    }
    fiche.update(techniques)
    return fiche, rapport


def _erreur(exc):
    """
    Erreur de validation, en français.

    Les messages de `jsonschema` sont en anglais et l'interface est en français,
    donc on rend un message traduit et on garde l'original sous
    `message_jsonschema` — pour le débogage seulement, jamais affiché.
    """
    chemin = "/".join(str(p) for p in exc.path)
    return {
        "chemin": chemin or "(racine)",
        "validateur": exc.validator,
        "valeur": exc.instance if isinstance(exc.instance, (str, int, float, bool, type(None)))
                  else None,
        "message": _MESSAGES.get(exc.validator, "valeur non conforme au schéma"),
        "message_jsonschema": exc.message[:300],
    }


def resumer(rapport):
    """Résumé court et français, destiné à une colonne d'Excel et à l'interface."""
    if not rapport:
        return ""
    statut = rapport.get("statut")
    if statut == "conforme":
        return "conforme"
    morceaux = []
    erreurs = rapport.get("erreurs") or []
    corrections = rapport.get("corrections") or []
    if erreurs:
        details = "; ".join(f"{e['chemin']} : {e['message']}" for e in erreurs[:3])
        morceaux.append(f"{len(erreurs)} erreur(s) — {details}"
                        + (" …" if len(erreurs) > 3 else ""))
    if corrections:
        par_regle = {}
        for c in corrections:
            par_regle[c["regle"]] = par_regle.get(c["regle"], 0) + 1
        morceaux.append("recalages : " + ", ".join(
            f"{regle} ×{n}" for regle, n in sorted(par_regle.items())))
    return " | ".join(morceaux)


def resumer_json(texte):
    """`resumer` à partir de la colonne `validation_errors_json` relue."""
    if not texte:
        return ""
    try:
        return resumer(json.loads(texte))
    except (ValueError, TypeError):
        return ""


def compter_recalages(rapport):
    """Nombre de recalages réels, hors suppressions de vide et de doublon."""
    return sum(1 for c in (rapport.get("corrections") or [])
               if c["regle"] not in ("vide_supprime", "doublon_supprime"))
