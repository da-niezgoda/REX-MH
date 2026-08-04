"""
Faux client Mistral, partagé par toute la suite.

Il y en avait deux, divergents : un dans `check_concurrence.py` (concurrence,
erreurs, cache) et un dans `check_integration.py` (segmentation, lot, fichiers).
Un seul jeu de classes couvre les deux usages, paramétré par ce dont chaque
appelant a besoin.

Les exceptions levées sont les VRAIES du SDK, pour que `pipeline.classer_erreur`
soit exercée comme en production : un `SDKError` 429 doit ressortir en « quota »
réessayable, un `AttributeError` en « bug » non réessayable.
"""
import json
import threading

import httpx
from mistralai.client.errors import SDKError

JETONS_PROMPT = 12_000
JETONS_CACHE = 8_832
JETONS_GENERATION = 900
MODELE_SERVI = "mistral-medium-2508"


class Inverseur:
    """
    Force un achèvement DÉSORDONNÉ sans dépendre du temps.

    L'ancienne version dormait `0.05 if page % 2 else 0.01` et pariait sur
    l'ordonnanceur : le test « l'ordre document est rétabli malgré un achèvement
    désordonné » ne valait alors que si le pari tenait. Ici les appels sont
    appariés et, dans chaque paire, **le second arrivé repart le premier** — ce
    qui est exactement la condition à tester, obtenue par construction.

    Le `wait` est borné : un appel resté sans partenaire (nombre impair, ou
    partenaire parti en exception) repart au bout du délai. Aucun interblocage
    possible, au pire une attente.
    """

    def __init__(self, delai=0.5):
        self.delai = delai
        self.verrou = threading.Lock()
        self.en_attente = None

    def croiser(self):
        with self.verrou:
            if self.en_attente is None:
                self.en_attente = threading.Event()
                mien, partenaire = self.en_attente, None
            else:
                mien, partenaire = None, self.en_attente
                self.en_attente = None
        if partenaire is not None:
            # Je suis le second : je libère le premier et je repars devant lui.
            partenaire.set()
        else:
            mien.wait(timeout=self.delai)

    def liberer(self):
        """À appeler dans un `finally` : ne laisse jamais un partenaire attendre."""
        with self.verrou:
            reste, self.en_attente = self.en_attente, None
        if reste is not None:
            reste.set()


def _reponse(contenu, caches):
    message = type("M", (), {"content": contenu})()
    usage = type("U", (), {"prompt_tokens": JETONS_PROMPT,
                           "completion_tokens": JETONS_GENERATION,
                           "total_tokens": JETONS_PROMPT + JETONS_GENERATION,
                           "prompt_tokens_details": {"cached_tokens": caches}})()
    return type("R", (), {"choices": [type("C", (), {"message": message})()],
                          "usage": usage, "model": MODELE_SERVI})()


class FauxChat:
    """
    Journalise l'ordre d'envoi et la concurrence réelle, comme le ferait l'API.

    `fiche` reçoit le numéro de la première page du segment et renvoie la charge
    à sérialiser. `segments`, s'il est fourni, est renvoyé pour les appels au
    modèle de segmentation.
    """

    def __init__(self, *, fiche=None, segments=None, echouer=(), lever=(),
                 cle_attendue=None, inverseur=None):
        self.fiche = fiche or (lambda page: {"a": f"fiche p{page}"})
        self.segments = segments
        self.echouer, self.lever = set(echouer), set(lever)
        self.cle_attendue = cle_attendue
        self.inverseur = inverseur
        self.appels, self.cles, self.pages = [], [], []
        self.en_vol = self.max_en_vol = 0
        self.par_cle = {}
        self.verrou = threading.Lock()

    def complete(self, **kw):
        import pipeline

        cle = kw.get("prompt_cache_key")
        assert cle, "prompt_cache_key doit être transmis"
        if self.cle_attendue is not None:
            assert cle == self.cle_attendue, (cle, self.cle_attendue)
        assert kw["temperature"] == 0.0, kw["temperature"]
        assert kw["random_seed"] == pipeline.RANDOM_SEED, kw["random_seed"]

        segmentation = (self.segments is not None
                        and kw["model"] == pipeline.MODEL_SEGMENTATION)
        page = None
        if not segmentation:
            page = json.loads(kw["messages"][1]["content"])["pages"][0]["page_number"]

        with self.verrou:
            self.appels.append(kw["model"])
            self.cles.append(cle)
            self.pages.append(page)
            self.par_cle[cle] = self.par_cle.get(cle, 0) + 1
            rang = self.par_cle[cle]
            self.en_vol += 1
            self.max_en_vol = max(self.max_en_vol, self.en_vol)

        try:
            if segmentation:
                return _reponse(json.dumps(self.segments), 0)
            if self.inverseur is not None:
                self.inverseur.croiser()
            if page in self.lever:
                # AttributeError : la classe d'erreur qu'un thread sans
                # ScriptRunContext obtient en lisant st.session_state.
                raise AttributeError('st.session_state has no attribute "REXPrompt"')
            if page in self.echouer:
                raise SDKError("saturé", httpx.Response(
                    429, request=httpx.Request("POST", "https://api.mistral.ai")))
            # Le cache touche dès le deuxième appel portant la même clé — ce que
            # l'échauffement séquentiel garantit en production.
            return _reponse(json.dumps(self.fiche(page)),
                            JETONS_CACHE if rang > 1 else 0)
        finally:
            with self.verrou:
                self.en_vol -= 1
            if self.inverseur is not None:
                self.inverseur.liberer()


class FauxFichiers:
    def __init__(self):
        self.televerses, self.supprimes, self.telecharges = [], [], []
        # Initialisé, contrairement à l'ancienne version : `download` lisait un
        # attribut jamais posé et aurait levé un AttributeError si le chemin de
        # récolte par fichier avait été emprunté.
        # `contenus` permet de servir une charge DIFFÉRENTE par file_id, ce qu'il
        # faut pour distinguer un fichier de sortie valide d'un fichier d'erreurs
        # illisible ; `contenu` reste le défaut.
        self.contenu = ""
        self.contenus = {}

    def upload(self, **kw):
        self.televerses.append(kw)
        return type("F", (), {"id": f"file-{len(self.televerses)}"})()

    def get_signed_url(self, **kw):
        return type("S", (), {"url": "https://signed"})()

    def delete(self, **kw):
        self.supprimes.append(kw["file_id"])

    def download(self, **kw):
        self.telecharges.append(kw["file_id"])
        return type("R", (), {"text": self.contenus.get(kw["file_id"], self.contenu)})()


class FauxOcr:
    def __init__(self, reponse):
        self.reponse, self.appels = reponse, 0

    def process(self, **kw):
        self.appels += 1
        assert kw["include_blocks"] is True, "les paramètres OCR doivent passer"
        return self.reponse


class FauxJobs:
    """
    `sorties` alimente le chemin « inline ». Poser `output_file` / `error_file`
    force au contraire le chemin de REPLI par téléchargement de fichier — celui
    qui s'exécute justement quand l'inline n'est pas disponible, et qu'il serait
    donc malvenu de laisser sans test.
    """

    def __init__(self):
        self.crees, self.sorties = [], []
        self.output_file = self.error_file = None
        self.statut = "SUCCESS"
        self.erreurs = []

    def create(self, **kw):
        self.crees.append(kw)
        return type("J", (), {"id": "batch-1", "status": "QUEUED"})()

    def get(self, **kw):
        return type("J", (), {"id": kw["job_id"], "status": self.statut,
                              "outputs": self.sorties,
                              "output_file": self.output_file,
                              "error_file": self.error_file,
                              "errors": self.erreurs,
                              "total_requests": len(self.sorties),
                              "succeeded_requests": len(self.sorties),
                              "failed_requests": 0})()


class FauxClient:
    """
    Client complet. `ocr=None` laisse la sous-API OCR absente, ce qui suffit aux
    tests de concurrence qui n'appellent que `chat`.
    """

    def __init__(self, *, ocr=None, **kw):
        self.chat = FauxChat(**kw)
        self.files = FauxFichiers()
        self.ocr = FauxOcr(ocr) if ocr is not None else None
        self.batch = type("B", (), {"jobs": FauxJobs()})()
