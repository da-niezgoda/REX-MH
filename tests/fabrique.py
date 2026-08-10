"""
Fabrique d'instances REX, bâtie DEPUIS `REX.schema.json` et non écrite en dur.

Le schéma compte 10 sections, 33 champs et 12 énumérations métier : une fixture
figée se désynchroniserait au premier ajout de champ (tâche 5). Tout ce qui suit
se recalcule donc à partir du schéma courant.

Généralise le `_instance_conforme` de l'ancien `check_integration.py`, dont les
limites étaient : tableaux toujours à un seul élément, et pas de moyen de
produire une instance délibérément invalide.
"""
import re
import unicodedata

# Candidats essayés dans l'ordre pour satisfaire un `pattern` du schéma. Évite
# d'inscrire les motifs en dur ici : c'est le schéma qui décide.
CANDIDATS_MOTIF = ("", "2020", "01/03/2020", "https://example.org/rex", "3-5")


def instance_conforme(noeud, *, taille_tableau=1, index_enum=0):
    """
    Plus petite instance valide d'un (sous-)schéma.

    `taille_tableau` permet d'obtenir plus d'un élément — nécessaire pour tester
    la jointure des listes à l'export, que l'ancienne version obligeait à
    surcharger à la main. `index_enum` choisit la valeur d'énumération, ce qui
    rend les instances variées mais toujours déterministes.
    """
    if noeud.get("type") == "object":
        proprietes = noeud.get("properties", {})
        return {cle: instance_conforme(proprietes[cle], taille_tableau=taille_tableau,
                                       index_enum=index_enum)
                for cle in noeud.get("required", [])}
    if noeud.get("type") == "array":
        items = noeud["items"]
        valeurs = [instance_conforme(items, taille_tableau=taille_tableau, index_enum=i)
                   for i in range(taille_tableau)]
        # Un tableau d'énumération sans doublon : `uniqueItems` a été retiré du
        # schéma pour le mode strict, mais une fixture avec doublons rendrait les
        # assertions de jointure ambiguës.
        vues, uniques = set(), []
        for v in valeurs:
            if isinstance(v, str) and v in vues:
                continue
            vues.add(v if isinstance(v, str) else id(v))
            uniques.append(v)
        return uniques
    if noeud.get("enum"):
        enum = noeud["enum"]
        return enum[index_enum % len(enum)]
    if noeud.get("type") == "string":
        motif = noeud.get("pattern")
        if not motif:
            return ""
        for candidat in CANDIDATS_MOTIF:
            if re.fullmatch(motif, candidat):
                return candidat
        raise AssertionError(f"aucun candidat ne satisfait le motif {motif!r}")
    if noeud.get("type") in ("integer", "number"):
        return noeud.get("minimum", 0)
    return ""


def _poser(fiche, chemin, valeur):
    """Pose une valeur repérée par « Section/champ »."""
    section, champ = chemin.split("/", 1)
    fiche.setdefault(section, {})[champ] = valeur


def fiche_conforme(schema, *, taille_tableau=1, **surcharges):
    """
    Fiche valide, puis surchargée là où un test doit vérifier un cas précis.

    Les surcharges sont nommées « Section__champ » (double tiret bas, parce que
    les mots-clés Python n'admettent pas la barre oblique) et acceptent aussi la
    forme « Section/champ » via `surcharges_chemins`.
    """
    fiche = instance_conforme(schema, taille_tableau=taille_tableau)
    chemins = surcharges.pop("surcharges_chemins", {}) or {}
    for cle, valeur in surcharges.items():
        _poser(fiche, cle.replace("__", "/"), valeur)
    for cle, valeur in chemins.items():
        _poser(fiche, cle, valeur)
    return fiche


# Valeur volontairement approximative que le faux modèle renvoie : casse et
# accent perdus. La couche de conformité doit la ramener à « Réserve Naturelle
# Régionale » (valeur conservée par la tâche 5), donc le run d'intégration sort en
# « corrigé » — ce qui exerce la chaîne complète jusqu'à la base, plutôt qu'un
# chemin où rien n'a jamais besoin d'être recalé.
CONTEXTE_APPROXIMATIF = "reserve naturelle regionale"
CONTEXTE_ATTENDU = "Réserve Naturelle Régionale"


def fiche_de_test(schema):
    """
    La fiche que les tests d'intégration utilisent : conforme après normalisation,
    avec deux éléments dans chaque tableau et des caractères à échapper dans le
    rendu HTML.
    """
    fiche = fiche_conforme(schema, taille_tableau=2)
    fiche["Presentation"]["Titre"] = "Restauration <tourbière> & marais"
    fiche["Presentation"]["Nom de l'organisme"] = "OiEau"
    # Format AAAA (année seule) depuis la tâche 5 ; le motif du schéma le contraint.
    fiche["Enjeux"]["date_debut"] = "2019"
    fiche["Enjeux"]["date_fin"] = "2021"
    fiche["Valorisation"]["url"] = "https://example.org/rex"
    fiche["Contexte"]["contexte"] = CONTEXTE_APPROXIMATIF
    return fiche


def variantes_proches(valeur):
    """
    Mutations MÉCANIQUES d'une valeur d'énumération, toutes censées se recaler sur
    elle. C'est la pièce maîtresse des tests de conformité : appliquée aux
    186 valeurs du schéma, elle donne deux balayages complémentaires —

      · rappel    : chaque mutation revient bien à SA valeur ;
      · précision : aucune mutation de A ne se résout en B ≠ A.

    Le second est le garde-fou anti-corruption, et c'est celui qu'une simple
    liste de cas écrite à la main n'aurait jamais couvert. Aucune fixture, aucun
    appel API, et l'ensemble continue de valoir quand la tâche 5 réécrira les
    énumérations.
    """
    if not valeur:
        return []
    variantes = {
        valeur.upper(),
        valeur.lower(),
        valeur.replace("-", " "),
        valeur.replace(" ", "-"),
        f"  {valeur} ",
        valeur.replace(" ", " "),          # espace insécable
        f"{valeur}­",                       # trait d'union conditionnel
        "".join(c for c in unicodedata.normalize("NFKD", valeur)
                if not unicodedata.combining(c)),          # accents retirés
        valeur.replace("'", "").replace("’", ""),          # apostrophes retirées
        re.sub(r"\bd([aeiouyéèêàâ])", r"d'\1", valeur),    # apostrophes rajoutées
    }
    # Vocabulaire codé « U - Tourbières non boisées » : le code seul et le
    # libellé seul doivent tous deux ramener à la valeur complète.
    code_libelle = re.match(r"^([0-9A-Za-z()]{1,6}) - (.+)$", valeur)
    if code_libelle:
        variantes.update(code_libelle.groups())
    return sorted(v for v in variantes if v and v != valeur)


def chemins_enumeres(schema, prefixe=""):
    """
    Tous les emplacements d'énumération du schéma, en « Section/champ ».

    Un champ dont les éléments de tableau sont énumérés est rendu sous la forme
    « Section/champ[] », pour distinguer un scalaire d'une liste.
    """
    trouves = {}
    for nom, noeud in (schema.get("properties") or {}).items():
        chemin = f"{prefixe}{nom}"
        if noeud.get("type") == "object":
            trouves.update(chemins_enumeres(noeud, prefixe=f"{chemin}/"))
        elif noeud.get("type") == "array" and (noeud.get("items") or {}).get("enum"):
            trouves[f"{chemin}[]"] = noeud["items"]["enum"]
        elif noeud.get("enum"):
            trouves[chemin] = noeud["enum"]
    return trouves
