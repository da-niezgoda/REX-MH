#!/usr/bin/env python
"""
Générateur de faux recueils REX : charges OCR synthétiques + vérité terrain.

But — éprouver la segmentation (énumérer→vérifier→raffiner) à plusieurs échelles
(de 3 à 24 fiches) AVEC les distracteurs qui la piègent en pratique : le sommaire
(« Liste des actions décrites ») et les cartes de localisation. C'est exactement
ce sur quoi le recueil réel faisait sur-découper puis, à grande échelle,
sous-découper — et où le premier essai (fenêtrage) plafonnait en rappel.

Le contenu est calqué sur le vrai recueil Rhin-Meuse (mêmes titres de projets,
même entête de fiche : titre en `#`, Objectif/Maître d'ouvrage/Surface/Montant en
gras, puis Contexte/Enjeux/Génie écologique) et pioche des valeurs LÉGALES dans
`REX.schema.json`. Tout est DÉTERMINISTE (aucun hasard) : la vérité terrain est
donc exacte et le corpus rejouable à l'identique.

Chaque page-vérité est classée : soit dans `fiches_attendues` (une fiche réelle),
soit dans `hors_projet` (intro, sommaire, carte). L'union des deux couvre TOUTES
les pages, sans trou ni recouvrement — invariant vérifié par tests/test_corpus.py.

    .venv/bin/python tests/corpus/_generateur.py     # (re)génère les corpus
"""
import hashlib
import json
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent.parent
CORPUS = RACINE / "tests/corpus"

# 28 titres réels, relevés sur le recueil de 129 pages (segmentation fenêtrée).
TITRES = [
    "Vallée alluviale de la Moselle sauvage de Virecourt à Chamagne",
    "Vallée de la Meurthe de Bertrichamps à Saint-Clément",
    "Zone inondable de la Thur entre Vieux-Thann et Cernay",
    "Réserve naturelle de la Petite Camargue Alsacienne",
    "Marais de Chaumont-devant-Damvillers",
    "Le Bassin Potassique de Haute-Alsace",
    "Prairies humides du ried de l'Ill à Sélestat",
    "Prairies humides de la Doller à Mulhouse",
    "Étang d'Amel",
    "L'étang de la Laixière à Moussey",
    "Coteau forestier du Bambois à Saulxures-sur-Moselotte",
    "Tourbière de Seuchaux aux Arrentès-de-Corcieux",
    "Delta du ruisseau Saint-Jacques et herbiers du lac de Gérardmer",
    "Les anciens bras du Rhin : le Breitsandgiessen à Rhinau",
    "Étang de Lindre",
    "Étangs de la ligne Maginot aquatique à Puttelange-aux-Lacs",
    "Zones humides de la plaine de la Woëvre",
    "Un ancien bras de l'Ill à Sermersheim",
    "Ancien bras vif de la Fecht à Bennwihr",
    "Les noues de la Meuse entre Verdun et Stenay",
    "Restauration des reculées de la Moselle entre Épinal et Chamagne",
    "Restauration de la Petite Camargue Alsacienne (renaturation)",
    "Restauration des anciens bras du Rhin : l'Eiswasser à Kunheim",
    "Restauration des étangs Néra à Altenach et Saint-Ulrich",
    "Étang du Bois de Générose à Courcelles-Chaussy",
    "Mares et dépressions humides du Bruch de l'Andlau",
    "Fossé de dérivation du Dollerbaechlein à Lutterbach",
    "Mare sur le site du Richtsendel à Erstein",
]

ORGANISMES = [
    "Conservatoire des Sites lorrains",
    "Conservatoire des espaces naturels d'Alsace",
    "Parc naturel régional de Lorraine",
    "Conservatoire du littoral",
    "Fédération de pêche de la Moselle",
    "Syndicat mixte du bassin de la Meurthe",
]


def _enums():
    """Valeurs légales piochées dans REX.schema.json (le « lire les valeurs »)."""
    schema = json.loads((RACINE / "REX.schema.json").read_text(encoding="utf-8"))

    def liste(section, champ):
        noeud = schema["properties"][section]["properties"][champ]
        valeurs = noeud.get("enum") or (noeud.get("items") or {}).get("enum") or []
        return [v for v in valeurs if v and v != "N/A"]

    return {
        "region": liste("Presentation", "Région"),
        "genie": liste("Typologie", "type_genie_ecologique"),
        "enjeux": liste("Enjeux", "enjeux"),
        "contexte": liste("Contexte", "contexte"),
        "ramsar": liste("Typologie", "type_milieu_ramsar"),
        "sdage": liste("Typologie", "type_milieu_sdage"),
    }


ENUMS = _enums()


def _cycle(cle, i):
    valeurs = ENUMS[cle]
    return valeurs[i % len(valeurs)]


# --- Pages ------------------------------------------------------------------

INTRO = [
    "# Comment intervenir en faveur des zones humides sur le bassin Rhin-Meuse ?\n\n"
    "## Quelques exemples de réalisations\n\n"
    "### Qu'est-ce qu'une zone humide ?\n\n"
    "Au sens de la loi sur l'eau de 1992, les zones humides sont des terrains "
    "exploités ou non, habituellement inondés ou gorgés d'eau douce, salée ou "
    "saumâtre de façon permanente ou temporaire. Elles rendent de nombreux "
    "services : épuration de l'eau, régulation des crues, réservoir de "
    "biodiversité.\n",
    "### Préservation, gestion, restauration\n\n"
    "La préservation par la maîtrise foncière met un site à l'abri durablement. "
    "La restauration et la renaturation sont mises en œuvre lorsque les zones "
    "humides ont disparu ou ont été dégradées (drainage, remblaiement, mise en "
    "culture).\n\n"
    "### Des cas concrets ?\n\n"
    "Cette brochure présente un recueil d'expériences conduites sur le bassin. "
    "Chaque fiche décrit un site, son maître d'ouvrage, les actions menées et "
    "leur coût.\n",
]


def _page_sommaire(fiches_meta):
    lignes = "".join(
        f"| {i + 1} | {m['titre_indicatif']} | p. {m['page_debut']}-{m['page_fin']} |\n"
        for i, m in enumerate(fiches_meta)
    )
    return (
        "## Liste des actions décrites\n\n"
        "Le tableau ci-dessous récapitule les opérations présentées dans ce "
        "recueil ; il ne s'agit pas d'une fiche de projet.\n\n"
        "| N° | Intitulé de l'opération | Pages |\n|----|----|----|\n" + lignes
    )


def _page_carte(legende):
    return (
        f"## {legende}\n\n"
        "![carte](carte.jpeg)\n\n"
        "*Carte de localisation des opérations décrites dans le recueil. Les "
        "points numérotés renvoient aux fiches correspondantes.*\n"
    )


def _pages_fiche(rang):
    """2 à 4 pages markdown d'une fiche. Entête + titre sur la 1re, prose ensuite."""
    titre = TITRES[rang % len(TITRES)]
    region = _cycle("region", rang)
    genie = [_cycle("genie", rang), _cycle("genie", rang + 7)]
    enjeux = [_cycle("enjeux", rang), _cycle("enjeux", rang + 4)]
    organisme = ORGANISMES[rang % len(ORGANISMES)]
    surface = 40 + rang * 23
    montant = (rang + 1) * 130000

    p1 = (
        f"# {titre}\n\n"
        f"**Objectif :** Préserver et restaurer durablement le site « {titre} » "
        f"par la maîtrise foncière et une gestion écologique adaptée.\n\n"
        f"**Maître d'ouvrage :** {organisme}\n\n"
        f"**Région :** {region}\n\n"
        f"**Surface :** {surface} ha\n\n"
        f"**Montant des acquisitions :** {montant} euros\n\n"
        f"## Contexte\n\n"
        f"Le site « {titre} » constitue un secteur remarquable du bassin "
        f"Rhin-Meuse. Sa mobilité hydrologique et ses milieux alluviaux "
        f"abritent une faune et une flore patrimoniales. L'opération vise à en "
        f"rétablir le bon fonctionnement.\n"
    )
    p2 = (
        f"## Enjeux eau, biodiversité et climat\n\n"
        + "".join(f"- {e}\n" for e in enjeux)
        + f"\n## Description des opérations\n\n"
        f"Sur le site « {titre} », le maître d'ouvrage a conduit l'acquisition "
        f"foncière puis un programme de gestion : restauration hydraulique, "
        f"reconquête des prairies humides et suivi scientifique pluriannuel.\n"
    )
    p3 = (
        f"## Génie écologique mobilisé\n\n"
        + "".join(f"- {g}\n" for g in genie)
        + f"\n## Typologie du milieu\n\n"
        f"- Ramsar : {_cycle('ramsar', rang)}\n"
        f"- SDAGE : {_cycle('sdage', rang)}\n\n"
        f"Le suivi montre une amélioration de la qualité de l'eau et un retour "
        f"des cortèges d'espèces inféodées à « {titre} ».\n"
    )
    p4 = (
        f"## Valorisation et perspectives\n\n"
        f"Les résultats obtenus sur « {titre} » ont fait l'objet de "
        f"communications et de sorties pédagogiques. Le partenariat se "
        f"poursuit pour pérenniser la gestion du site.\n"
    )
    return [p1, p2, p3, p4][: 2 + (rang % 3)]


# --- Assemblage d'un corpus --------------------------------------------------


def construire_corpus(nom, nb_fiches, *, avec_sommaire, cartes_apres=()):
    """Assemble un faux recueil ; renvoie {nom, rex, nb_pages, ocr, verite}."""
    pages, hors, fiches_meta = [], [], []

    def ajouter(markdown):
        pages.append(markdown)
        return len(pages)   # numéro de page 1-indexé de la page ajoutée

    a, b = ajouter(INTRO[0]), ajouter(INTRO[1])
    hors.append({"pages": [a, b],
                 "motif": "introduction : définition d'une zone humide, types d'intervention"})

    idx_sommaire = None
    if avec_sommaire:
        idx_sommaire = ajouter("")   # place réservée, remplie après le calcul des pages
        hors.append({"pages": [idx_sommaire],
                     "motif": "« Liste des actions décrites » : tableau de sommaire"})
        pc = ajouter(_page_carte("Localisation des opérations décrites"))
        hors.append({"pages": [pc],
                     "motif": "« Localisation des opérations décrites » : carte"})

    for rang in range(nb_fiches):
        debut = len(pages) + 1
        for md in _pages_fiche(rang):
            ajouter(md)
        fiches_meta.append({
            "reference": f"Fiche {rang + 1}",
            "titre_indicatif": TITRES[rang % len(TITRES)],
            "page_debut": debut,
            "page_fin": len(pages),
        })
        if rang in cartes_apres:
            pc = ajouter(_page_carte(f"Carte du site : {TITRES[rang % len(TITRES)]}"))
            hors.append({"pages": [pc], "motif": "carte intercalaire entre deux fiches"})

    if idx_sommaire is not None:
        pages[idx_sommaire - 1] = _page_sommaire(fiches_meta)

    ocr = {"pages": [{"index": i, "markdown": md} for i, md in enumerate(pages)],
           "model": "synthetique-corpus"}
    sha = hashlib.sha256("\n".join(pages).encode("utf-8")).hexdigest()
    verite = {
        "version": 1,
        "fichier": nom,
        "document_sha256": sha,
        "nb_pages": len(pages),
        "fiches_attendues": fiches_meta,
        "hors_projet": hors,
        "champs": {},
    }
    return {"nom": nom, "rex": len(fiches_meta), "nb_pages": len(pages),
            "ocr": ocr, "verite": verite}


# Les six recueils : nombre de REX croissant, distracteurs de plus en plus nombreux.
PLAN = [
    ("corpus_1_3rex", 3, {"avec_sommaire": False, "cartes_apres": ()}),
    ("corpus_2_5rex", 5, {"avec_sommaire": True, "cartes_apres": (1,)}),
    ("corpus_3_8rex", 8, {"avec_sommaire": True, "cartes_apres": (2, 5)}),
    ("corpus_4_12rex", 12, {"avec_sommaire": True, "cartes_apres": (3, 7)}),
    ("corpus_5_18rex", 18, {"avec_sommaire": True, "cartes_apres": (4, 9, 14)}),
    ("corpus_6_24rex", 24, {"avec_sommaire": True, "cartes_apres": (5, 11, 17)}),
]


# --- Rendu PDF ---------------------------------------------------------------
#
# Un vrai PDF par corpus (une page markdown = une page PDF), en plus du JSON, pour
# éprouver le pipeline OCR RÉEL et pas seulement une charge OCR injectée. Rendu par
# matplotlib (déjà installé ; ni reportlab ni weasyprint ici) : texte vectoriel net
# que l'OCR de Mistral relit sans peine. Les PDF sont des fixtures committées et
# régénérables ; ce rendu n'a pas besoin d'être joli, juste lisible et structuré
# (titres `#` distincts pour les frontières, « Liste des actions décrites » et
# « Localisation » pour les distracteurs hors-projet).

_H_A4 = 11.69 * 72   # hauteur A4 en points, pour convertir la taille de police en pas


def _rendre_page(pdf, markdown):
    import textwrap

    import matplotlib.patches as patches
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    x, etat = 0.09, {"y": 0.955}

    def ligne(txt, size=10, bold=False, indent=0.0, gris=False, saut_avant=0.0):
        etat["y"] -= saut_avant
        ax.text(x + indent, etat["y"], txt, fontsize=size,
                fontweight="bold" if bold else "normal", family="DejaVu Sans",
                color="0.4" if gris else "black", ha="left", va="top")
        etat["y"] -= size * 1.75 / _H_A4

    for raw in markdown.split("\n"):
        line = raw.rstrip()
        if not line:
            etat["y"] -= 0.012
        elif line.startswith("# "):
            ligne(line[2:], size=15, bold=True, saut_avant=0.004)
            etat["y"] -= 0.006
        elif line.startswith("## "):
            ligne(line[3:], size=12, bold=True, saut_avant=0.006)
        elif line.startswith("### "):
            ligne(line[4:], size=11, bold=True, saut_avant=0.003)
        elif line.startswith("- "):
            for i, w in enumerate(textwrap.wrap(line[2:], 86) or [""]):
                ligne(("• " if i == 0 else "  ") + w, indent=0.02)
        elif line.startswith("!["):                       # image (carte) → placeholder gris
            h = 0.20
            ax.add_patch(patches.Rectangle((x, etat["y"] - h), 0.82, h,
                         facecolor="0.85", edgecolor="0.6"))
            ax.text(0.5, etat["y"] - h / 2, "[carte de localisation]", fontsize=11,
                    color="0.5", ha="center", va="center")
            etat["y"] -= h + 0.012
        elif line.startswith("|"):                         # ligne de tableau
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):          # séparateur markdown
                continue
            ligne("    ".join(cells), size=9)
        else:                                               # texte / champ **gras**
            gras = line.startswith("**")
            for i, w in enumerate(textwrap.wrap(line.replace("**", ""), 95) or [""]):
                ligne(w, size=10.5 if gras else 10, bold=gras and i == 0)

    pdf.savefig(fig)
    plt.close(fig)


def ecrire_pdf(pages_markdown, chemin):
    """Un PDF multi-pages : une page markdown → une page PDF."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.backends.backend_pdf import PdfPages

    with PdfPages(chemin) as pdf:
        for md in pages_markdown:
            _rendre_page(pdf, md)


def build_all():
    CORPUS.mkdir(parents=True, exist_ok=True)
    manifeste = []
    for nom, n, opts in PLAN:
        corpus = construire_corpus(nom, n, **opts)
        (CORPUS / f"{nom}.json").write_text(
            json.dumps(corpus, ensure_ascii=False, indent=1), encoding="utf-8")
        pages_md = [p["markdown"] for p in corpus["ocr"]["pages"]]
        ecrire_pdf(pages_md, CORPUS / f"{nom}.pdf")
        manifeste.append({"nom": nom, "fichier": f"{nom}.json", "pdf": f"{nom}.pdf",
                          "rex": corpus["rex"], "nb_pages": corpus["nb_pages"]})
        print(f"  {nom}: {corpus['rex']} REX, {corpus['nb_pages']} pages  (json + pdf)")
    (CORPUS / "manifest.json").write_text(
        json.dumps(manifeste, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  manifeste : {len(manifeste)} corpus → {CORPUS / 'manifest.json'}")


if __name__ == "__main__":
    build_all()
