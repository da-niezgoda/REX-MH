"""
Persistance SQLite pour REX-MH : cache OCR, historique des traitements, travaux
par lot.

Ce module n'importe PAS streamlit. C'est délibéré : il reste ainsi testable hors
application (pytest, tâche 3), et la résolution du chemin de la base vit dans
`app.py`, là où `st.secrets` est disponible — exactement comme `get_api_key()`.

Modèle de données :

    documents   un PDF, identifié par le hash de son contenu (le nom de fichier
                n'est qu'informatif). `sha256` EST la clé du cache OCR.
    ocr_cache   la charge OCR gzippée, 1 pour 1 avec documents. Table séparée
                pour que lister l'historique ne charge jamais les blobs.
    runs        une tentative de traitement. Un document a N runs : c'est ce qui
                permet de comparer un avant/après édition de prompt.
    fiches      une ligne par segment, Y COMPRIS les échecs — c'est ce que
                parcourt « relancer les fiches en échec ».
    batch_jobs  état des travaux par lot, suffisant pour reprendre après la
                fermeture de l'onglet.
"""
import contextlib
import gzip
import json
import os
import re
import sqlite3
import threading
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = "data/rex.db"

# Statuts de travaux par lot qui demandent encore un suivi.
STATUTS_BATCH_OUVERTS = (
    "QUEUED",
    "RUNNING",
    "CANCELLATION_REQUESTED",
)
STATUTS_BATCH_TERMINAUX = (
    "SUCCESS",
    "FAILED",
    "TIMEOUT_EXCEEDED",
    "CANCELLED",
)

# Garde-fous de l'import d'archive. Vérifiés AVANT toute écriture.
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_DECOMPRESSE_BYTES = 400 * 1024 * 1024
MAX_DOCUMENTS = 500
MAX_FICHES = 20_000
FORMAT_ARCHIVE = "rex-historique"

_MEMBRES_ATTENDUS = {"manifest.json", "documents.json", "runs.json", "fiches.json"}
_NOM_MEMBRE_OCR = re.compile(r"^ocr/[0-9a-f]{64}\.json\.gz$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_STATUTS_RUN = ("en_cours", "termine", "partiel", "echec")
_MODES = ("rapide", "economique")
_STATUTS_FICHE = ("ok", "echec", "en_attente")

_LOCK = threading.RLock()
_DB_PATH = None
_INITIALISED = False


class BundleInvalide(Exception):
    """Archive d'historique refusée par les contrôles d'import."""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256     TEXT    NOT NULL UNIQUE,
    filename   TEXT    NOT NULL,
    size_bytes INTEGER,
    page_count INTEGER,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS ocr_cache (
    document_id     INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    cle_ocr         TEXT    NOT NULL,
    model           TEXT,
    payload_gz      BLOB    NOT NULL,
    payload_bytes   INTEGER NOT NULL,
    pages_processed INTEGER,
    avg_confidence  REAL,
    sdk_version     TEXT,
    invalid_reason  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid                        TEXT    NOT NULL UNIQUE,
    document_id                INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status                     TEXT    NOT NULL
        CHECK (status IN ('en_cours','termine','partiel','echec')),
    mode                       TEXT    NOT NULL CHECK (mode IN ('rapide','economique')),
    started_at                 TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    finished_at                TEXT,
    model_ocr                  TEXT,
    model_segmentation         TEXT,
    model_extraction           TEXT,
    prompt_extraction_sha256   TEXT,
    prompt_segmentation_sha256 TEXT,
    schema_rex_sha256          TEXT,
    schema_list_sha256         TEXT,
    segmentation_json          TEXT,
    prompt_tokens              INTEGER NOT NULL DEFAULT 0,
    cached_tokens              INTEGER NOT NULL DEFAULT 0,
    completion_tokens          INTEGER NOT NULL DEFAULT 0,
    error                      TEXT
);

CREATE TABLE IF NOT EXISTS fiches (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                 INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    document_id            INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq                    INTEGER NOT NULL,
    titre                  TEXT,
    page_debut             INTEGER,
    page_fin               INTEGER,
    status                 TEXT    NOT NULL CHECK (status IN ('ok','echec','en_attente')),
    data_json              TEXT,
    error                  TEXT,
    categorie              TEXT,
    model_extraction       TEXT,
    prompt_hash            TEXT,
    prompt_tokens          INTEGER,
    cached_tokens          INTEGER,
    completion_tokens      INTEGER,
    validation_status      TEXT,
    validation_errors_json TEXT,
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT    NOT NULL UNIQUE,
    run_id             INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    document_id        INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    endpoint           TEXT    NOT NULL,
    kind               TEXT    NOT NULL CHECK (kind IN ('segmentation','extraction','ocr')),
    status             TEXT    NOT NULL,
    is_terminal        INTEGER NOT NULL DEFAULT 0,
    input_file_id      TEXT,
    output_file_id     TEXT,
    error_file_id      TEXT,
    total_requests     INTEGER,
    succeeded_requests INTEGER,
    failed_requests    INTEGER,
    fiche_seq_map_json TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    polled_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_document ON runs (document_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status   ON runs (status);
CREATE INDEX IF NOT EXISTS idx_fiches_run    ON fiches (run_id, status);
CREATE INDEX IF NOT EXISTS idx_fiches_doc    ON fiches (document_id, seq);
CREATE INDEX IF NOT EXISTS idx_batch_open    ON batch_jobs (is_terminal, status);
"""

# Index partiel : au plus un run actif par document. C'est le vrai garde-fou
# contre la double facturation (double clic sur « Envoyer », deux visiteurs qui
# envoient le même PDF) — celui de l'interface n'est que cosmétique.
SCHEMA_SQL_UN_SEUL_EN_COURS = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_un_seul_en_cours
    ON runs (document_id) WHERE status = 'en_cours';
"""


# --- Cycle de vie ------------------------------------------------------------


def init_db(db_path=None):
    """
    Crée la base et son schéma si besoin. Idempotent.

    Ordre de résolution du chemin : argument → $REX_DB_PATH → data/rex.db.
    """
    global _DB_PATH, _INITIALISED

    chemin = db_path or os.environ.get("REX_DB_PATH") or DEFAULT_DB_PATH
    parent = Path(chemin).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    with _LOCK:
        _DB_PATH = str(chemin)
        con = sqlite3.connect(_DB_PATH, timeout=30.0)
        try:
            # Persistants, posés une seule fois dans l'en-tête du fichier.
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
            con.executescript(SCHEMA_SQL)
            con.executescript(SCHEMA_SQL_UN_SEUL_EN_COURS)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            con.commit()
        finally:
            con.close()
        _INITIALISED = True
    return _DB_PATH


def db_path():
    """Chemin de la base actuellement utilisée (None si init_db n'a pas tourné)."""
    return _DB_PATH


def schema_version():
    """Valeur de PRAGMA user_version."""
    with _connect() as con:
        return con.execute("PRAGMA user_version").fetchone()[0]


@contextlib.contextmanager
def _connect(write=False):
    """
    Connexion courte. En WAL les lecteurs ne bloquent pas l'écrivain : seules
    les écritures prennent le verrou de processus.

    `PRAGMA foreign_keys` N'EST PAS persistant — il doit être reposé à chaque
    connexion, sinon les ON DELETE CASCADE ne s'appliquent pas.
    """
    if not _INITIALISED:
        init_db()
    con = sqlite3.connect(_DB_PATH, timeout=30.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        if write:
            with _LOCK, con:  # `with con` = transaction : commit ou rollback
                yield con
        else:
            yield con
    finally:
        con.close()


def _rows(curseur):
    """Lignes en dicts simples — sqlite3.Row ne survit pas à la fermeture."""
    return [dict(r) for r in curseur.fetchall()]


def _row(curseur):
    ligne = curseur.fetchone()
    return dict(ligne) if ligne is not None else None


# --- Documents ---------------------------------------------------------------


def get_or_create_document(sha256, filename, size_bytes=None):
    """
    Identifiant du document pour ce contenu, créé si absent.

    L'unicité est garantie par la contrainte de base, pas par le code : deux
    onglets peuvent envoyer le même PDF en même temps.
    """
    with _connect(write=True) as con:
        con.execute(
            "INSERT INTO documents (sha256, filename, size_bytes) VALUES (?, ?, ?) "
            "ON CONFLICT(sha256) DO NOTHING",
            (sha256, filename, size_bytes),
        )
        return con.execute(
            "SELECT id FROM documents WHERE sha256 = ?", (sha256,)
        ).fetchone()[0]


def set_document_pages(document_id, page_count):
    """Nombre de pages, connu seulement après l'OCR."""
    with _connect(write=True) as con:
        con.execute(
            "UPDATE documents SET page_count = ? WHERE id = ?",
            (page_count, document_id),
        )


def get_document(document_id):
    with _connect() as con:
        return _row(
            con.execute("SELECT * FROM documents WHERE id = ?", (document_id,))
        )


def list_documents():
    """
    Documents et compteurs de runs, du plus récemment traité au plus ancien.

    Une seule requête, et jamais de blob : la charge OCR vit dans ocr_cache.
    """
    with _connect() as con:
        return _rows(
            con.execute(
                """
                SELECT d.id, d.sha256, d.filename, d.size_bytes, d.page_count,
                       d.created_at,
                       COUNT(r.id)                        AS nb_runs,
                       MAX(r.started_at)                  AS dernier_run,
                       (o.document_id IS NOT NULL)        AS a_cache_ocr,
                       o.payload_bytes                    AS ocr_bytes
                  FROM documents d
                  LEFT JOIN runs      r ON r.document_id = d.id
                  LEFT JOIN ocr_cache o ON o.document_id = d.id
                 GROUP BY d.id
                 ORDER BY COALESCE(MAX(r.started_at), d.created_at) DESC
                """
            )
        )


def delete_document(document_id):
    """Supprime le document, sa charge OCR, ses runs et ses fiches (cascade)."""
    with _connect(write=True) as con:
        con.execute("DELETE FROM documents WHERE id = ?", (document_id,))


# --- Cache OCR ---------------------------------------------------------------


def get_ocr_payload(document_id, cle_ocr=None):
    """
    Charge OCR décompressée (JSON en texte), ou None.

    `cle_ocr` inclut le modèle et les paramètres d'appel OCR : si elle ne
    correspond pas à celle enregistrée, on considère qu'il n'y a pas de cache —
    servir une charge sans blocs à une segmentation qui les attend serait pire
    que de repayer l'OCR. Une charge marquée invalide est ignorée de même.
    """
    with _connect() as con:
        ligne = con.execute(
            "SELECT cle_ocr, payload_gz, invalid_reason FROM ocr_cache "
            "WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    if ligne is None or ligne["invalid_reason"]:
        return None
    if cle_ocr is not None and ligne["cle_ocr"] != cle_ocr:
        return None
    return gzip.decompress(ligne["payload_gz"]).decode("utf-8")


def save_ocr_payload(
    document_id,
    payload,
    *,
    cle_ocr,
    model=None,
    pages_processed=None,
    avg_confidence=None,
    sdk_version=None,
):
    """
    Enregistre la charge OCR, gzippée, prise UNE SEULE FOIS sur la réponse
    vivante. Elle ne doit jamais être relue puis réécrite : le round-trip
    Pydantic n'est pas idempotent (les blocs de type inconnu s'imbriquent d'un
    niveau à chaque passe) et les clés que le SDK installé ne modélise pas sont
    silencieusement perdues. D'où `sdk_version` : un lecteur peut savoir qu'une
    charge est antérieure à une montée de version.
    """
    octets = payload.encode("utf-8")
    with _connect(write=True) as con:
        con.execute(
            """
            INSERT INTO ocr_cache (document_id, cle_ocr, model, payload_gz,
                                   payload_bytes, pages_processed, avg_confidence,
                                   sdk_version, invalid_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(document_id) DO UPDATE SET
                cle_ocr = excluded.cle_ocr,
                model = excluded.model,
                payload_gz = excluded.payload_gz,
                payload_bytes = excluded.payload_bytes,
                pages_processed = excluded.pages_processed,
                avg_confidence = excluded.avg_confidence,
                sdk_version = excluded.sdk_version,
                invalid_reason = NULL,
                created_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """,
            (
                document_id,
                cle_ocr,
                model,
                gzip.compress(octets),
                len(octets),
                pages_processed,
                avg_confidence,
                sdk_version,
            ),
        )


def get_ocr_meta(document_id):
    """Métadonnées du cache OCR, sans le blob."""
    with _connect() as con:
        return _row(
            con.execute(
                "SELECT document_id, cle_ocr, model, payload_bytes, pages_processed, "
                "avg_confidence, sdk_version, invalid_reason, created_at "
                "FROM ocr_cache WHERE document_id = ?",
                (document_id,),
            )
        )


def has_ocr_payload(document_id):
    with _connect() as con:
        return (
            con.execute(
                "SELECT 1 FROM ocr_cache WHERE document_id = ? AND invalid_reason IS NULL",
                (document_id,),
            ).fetchone()
            is not None
        )


def has_ocr_payload_pour_sha(sha256):
    """Le contenu de ce fichier est-il déjà océrisé ? (avant tout insert)"""
    with _connect() as con:
        return (
            con.execute(
                "SELECT 1 FROM ocr_cache o JOIN documents d ON d.id = o.document_id "
                "WHERE d.sha256 = ? AND o.invalid_reason IS NULL",
                (sha256,),
            ).fetchone()
            is not None
        )


def mark_ocr_payload_invalid(document_id, raison):
    """
    Marque une charge illisible pour ne pas la retenter à chaque run. L'appelant
    refait l'OCR ; il ne plante pas.
    """
    with _connect(write=True) as con:
        con.execute(
            "UPDATE ocr_cache SET invalid_reason = ? WHERE document_id = ?",
            (raison[:500], document_id),
        )


# --- Runs --------------------------------------------------------------------


def start_run(
    document_id,
    *,
    mode,
    prompt_extraction_sha256=None,
    prompt_segmentation_sha256=None,
    schema_rex_sha256=None,
    schema_list_sha256=None,
    segmentation_json=None,
    uid=None,
):
    """
    Ouvre un run et renvoie (run_id, uid).

    Les empreintes sont celles des prompts RENDUS (schéma déjà substitué) : une
    édition de prompt comme de schéma rend le run distinct, ce sur quoi la
    tâche 3 pourra grouper.

    Lève sqlite3.IntegrityError si un run est déjà en cours sur ce document —
    c'est le garde-fou de double facturation, à attraper côté appelant.
    """
    if mode not in _MODES:
        raise ValueError(f"mode inconnu : {mode!r}")
    uid = uid or str(uuid.uuid4())
    with _connect(write=True) as con:
        cur = con.execute(
            """
            INSERT INTO runs (uid, document_id, status, mode,
                              prompt_extraction_sha256, prompt_segmentation_sha256,
                              schema_rex_sha256, schema_list_sha256, segmentation_json)
            VALUES (?, ?, 'en_cours', ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                document_id,
                mode,
                prompt_extraction_sha256,
                prompt_segmentation_sha256,
                schema_rex_sha256,
                schema_list_sha256,
                segmentation_json,
            ),
        )
        return cur.lastrowid, uid


def set_run_segmentation(run_id, segmentation_json, *, model_segmentation=None):
    """
    Enregistre le découpage produit par la passe de segmentation.

    Il est porté par le run et non par le cache OCR : c'est une sortie de modèle
    et de prompt, pas une propriété du fichier. Le mettre en cache par document
    servirait silencieusement des bornes produites par un ancien listPrompt.md.
    """
    with _connect(write=True) as con:
        con.execute(
            "UPDATE runs SET segmentation_json = ?, "
            "model_segmentation = COALESCE(?, model_segmentation) WHERE id = ?",
            (segmentation_json, model_segmentation, run_id),
        )


def set_run_models(run_id, *, model_ocr=None, model_segmentation=None,
                   model_extraction=None):
    """Versions réellement servies par l'API, connues après les appels."""
    with _connect(write=True) as con:
        con.execute(
            """
            UPDATE runs SET model_ocr = COALESCE(?, model_ocr),
                            model_segmentation = COALESCE(?, model_segmentation),
                            model_extraction = COALESCE(?, model_extraction)
             WHERE id = ?
            """,
            (model_ocr, model_segmentation, model_extraction, run_id),
        )


def add_run_usage(run_id, *, prompt_tokens=0, cached_tokens=0, completion_tokens=0):
    """
    Cumule la consommation. Écrit en incrément (`= x + ?`) et non en
    affectation, pour qu'un comptage concurrent ne puisse pas perdre de jetons.
    """
    with _connect(write=True) as con:
        con.execute(
            """
            UPDATE runs SET prompt_tokens = prompt_tokens + ?,
                            cached_tokens = cached_tokens + ?,
                            completion_tokens = completion_tokens + ?
             WHERE id = ?
            """,
            (prompt_tokens, cached_tokens, completion_tokens, run_id),
        )


def finish_run(run_id, *, status, error=None):
    if status not in _STATUTS_RUN:
        raise ValueError(f"statut de run inconnu : {status!r}")
    with _connect(write=True) as con:
        con.execute(
            "UPDATE runs SET status = ?, error = ?, "
            "finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
            (status, error, run_id),
        )


def get_run(run_id):
    with _connect() as con:
        return _row(
            con.execute(
                """
                SELECT r.*, d.filename, d.sha256, d.page_count
                  FROM runs r JOIN documents d ON d.id = r.document_id
                 WHERE r.id = ?
                """,
                (run_id,),
            )
        )


def list_runs(document_id=None):
    """Runs, avec le compte de fiches par statut. Plus récent d'abord."""
    where, params = "", ()
    if document_id is not None:
        where, params = "WHERE r.document_id = ?", (document_id,)
    with _connect() as con:
        return _rows(
            con.execute(
                f"""
                SELECT r.id, r.uid, r.document_id, r.status, r.mode, r.started_at,
                       r.finished_at, r.model_extraction, r.prompt_extraction_sha256,
                       r.prompt_tokens, r.cached_tokens, r.completion_tokens, r.error,
                       d.filename,
                       SUM(CASE WHEN f.status = 'ok' THEN 1 ELSE 0 END)    AS nb_ok,
                       SUM(CASE WHEN f.status = 'echec' THEN 1 ELSE 0 END) AS nb_echec,
                       COUNT(f.id)                                        AS nb_fiches
                  FROM runs r
                  JOIN documents d ON d.id = r.document_id
                  LEFT JOIN fiches f ON f.run_id = r.id
                {where}
                 GROUP BY r.id
                 ORDER BY r.started_at DESC, r.id DESC
                """,
                params,
            )
        )


def list_open_runs():
    """Runs jamais clôturés — reliquats d'un crash ou lots en attente."""
    with _connect() as con:
        return _rows(
            con.execute(
                "SELECT r.*, d.filename FROM runs r JOIN documents d ON d.id = r.document_id "
                "WHERE r.status = 'en_cours' ORDER BY r.started_at DESC"
            )
        )


# --- Fiches ------------------------------------------------------------------


def upsert_fiche(
    run_id,
    document_id,
    seq,
    *,
    status,
    titre=None,
    page_debut=None,
    page_fin=None,
    data=None,
    error=None,
    categorie=None,
    model_extraction=None,
    prompt_hash=None,
    usage=None,
):
    """
    Écrit ou réécrit la fiche `seq` de ce run. Idempotent grâce à
    UNIQUE(run_id, seq) : c'est ce qui rend « relancer les fiches en échec » et
    « ré-extraire cette fiche » possibles sans dupliquer de lignes.

    Les six clés préfixées par « _ » que le pipeline injecte ne sont PAS
    stockées dans data_json : elles vivent en colonnes et sont réinjectées à la
    lecture. `REX.schema.json` a additionalProperties: false à la racine, donc
    une fiche qui les transporterait serait rejetée par la validation de la
    tâche 3.
    """
    if status not in _STATUTS_FICHE:
        raise ValueError(f"statut de fiche inconnu : {status!r}")
    usage = usage or {}
    propre = None
    if data is not None:
        propre = json.dumps(
            {k: v for k, v in data.items() if not k.startswith("_")},
            ensure_ascii=False,
        )
    with _connect(write=True) as con:
        con.execute(
            """
            INSERT INTO fiches (run_id, document_id, seq, titre, page_debut, page_fin,
                                status, data_json, error, categorie, model_extraction,
                                prompt_hash, prompt_tokens, cached_tokens,
                                completion_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, seq) DO UPDATE SET
                titre = excluded.titre,
                page_debut = excluded.page_debut,
                page_fin = excluded.page_fin,
                status = excluded.status,
                data_json = excluded.data_json,
                error = excluded.error,
                categorie = excluded.categorie,
                model_extraction = excluded.model_extraction,
                prompt_hash = excluded.prompt_hash,
                prompt_tokens = excluded.prompt_tokens,
                cached_tokens = excluded.cached_tokens,
                completion_tokens = excluded.completion_tokens,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """,
            (
                run_id,
                document_id,
                seq,
                titre,
                page_debut,
                page_fin,
                status,
                propre,
                error,
                categorie,
                model_extraction,
                prompt_hash,
                usage.get("prompt_tokens"),
                usage.get("cached_tokens"),
                usage.get("completion_tokens"),
            ),
        )


def list_fiches(run_id, status=None):
    where, params = "WHERE run_id = ?", [run_id]
    if status is not None:
        where += " AND status = ?"
        params.append(status)
    with _connect() as con:
        return _rows(
            con.execute(
                f"SELECT * FROM fiches {where} ORDER BY seq", tuple(params)
            )
        )


# --- Rechargement dans l'interface existante ---------------------------------


def load_run_as_parsed_data(run_id):
    """
    Recharge un run dans la forme que `display_results_table()` attend déjà :
    {'filename', 'date', 'projects', 'run_id'}.

    Les clés « _ » retirées à l'écriture sont réinjectées ici — source unique de
    vérité côté colonnes, `data_json` reste conforme au schéma.
    """
    run = get_run(run_id)
    if run is None:
        return None
    projects = []
    for fiche in list_fiches(run_id, status="ok"):
        if not fiche["data_json"]:
            continue
        data = json.loads(fiche["data_json"])
        data["_project_title"] = fiche["titre"]
        data["_page_debut"] = fiche["page_debut"]
        data["_page_fin"] = fiche["page_fin"]
        data["_segment_index"] = fiche["seq"]
        data["_model_ocr"] = run["model_ocr"]
        data["_model_segmentation"] = run["model_segmentation"]
        data["_model_extraction"] = fiche["model_extraction"] or run["model_extraction"]
        data["_prompt_hash"] = fiche["prompt_hash"]
        projects.append(data)
    return {
        "filename": run["filename"],
        "date": (run["finished_at"] or run["started_at"] or "").replace("T", " ")[:16],
        "projects": projects,
        "run_id": run_id,
        "document_id": run["document_id"],
    }


def load_failures(run_id):
    """
    Échecs d'un run dans la forme que le pipeline produit, pour que le panneau
    d'échecs et « relancer » fonctionnent aussi sur un run rechargé.
    """
    return [
        {
            "index": f["seq"],
            "titre": f["titre"],
            "pages": (f["page_debut"], f["page_fin"]),
            "categorie": f["categorie"],
            "error": f["error"],
        }
        for f in list_fiches(run_id, status="echec")
    ]


# --- Travaux par lot ---------------------------------------------------------


def record_batch_job(
    job_id,
    *,
    run_id,
    document_id,
    endpoint,
    kind,
    status,
    input_file_id=None,
    fiche_seq_map=None,
):
    """
    Ancre de reprise du mode économique. À écrire AVANT de rendre la main :
    si le processus meurt entre jobs.create() et cet enregistrement, le travail
    tourne côté Mistral et personne ne le récolte.

    `fiche_seq_map` associe chaque custom_id à son index de segment — sans elle,
    impossible de rattacher les réponses aux fiches après un redémarrage.
    """
    with _connect(write=True) as con:
        con.execute(
            """
            INSERT INTO batch_jobs (job_id, run_id, document_id, endpoint, kind,
                                    status, is_terminal, input_file_id,
                                    fiche_seq_map_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                is_terminal = excluded.is_terminal,
                input_file_id = excluded.input_file_id,
                fiche_seq_map_json = excluded.fiche_seq_map_json,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """,
            (
                job_id,
                run_id,
                document_id,
                endpoint,
                kind,
                status,
                int(status in STATUTS_BATCH_TERMINAUX),
                input_file_id,
                json.dumps(fiche_seq_map or {}),
            ),
        )


def refresh_batch_job(
    job_id,
    *,
    status,
    output_file_id=None,
    error_file_id=None,
    total_requests=None,
    succeeded_requests=None,
    failed_requests=None,
):
    """Met à jour l'état d'un travail après un sondage."""
    with _connect(write=True) as con:
        con.execute(
            """
            UPDATE batch_jobs
               SET status = ?, is_terminal = ?,
                   output_file_id = COALESCE(?, output_file_id),
                   error_file_id = COALESCE(?, error_file_id),
                   total_requests = COALESCE(?, total_requests),
                   succeeded_requests = COALESCE(?, succeeded_requests),
                   failed_requests = COALESCE(?, failed_requests),
                   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                   polled_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
             WHERE job_id = ?
            """,
            (
                status,
                int(status in STATUTS_BATCH_TERMINAUX),
                output_file_id,
                error_file_id,
                total_requests,
                succeeded_requests,
                failed_requests,
                job_id,
            ),
        )


def get_batch_job(job_id):
    with _connect() as con:
        return _row(
            con.execute(
                "SELECT b.*, d.filename FROM batch_jobs b "
                "JOIN documents d ON d.id = b.document_id WHERE b.job_id = ?",
                (job_id,),
            )
        )


def open_batch_jobs():
    """Travaux non terminaux, à afficher avec un bouton « Actualiser »."""
    with _connect() as con:
        return _rows(
            con.execute(
                "SELECT b.*, d.filename FROM batch_jobs b "
                "JOIN documents d ON d.id = b.document_id "
                "WHERE b.is_terminal = 0 ORDER BY b.created_at DESC"
            )
        )


def historique_stats():
    """Compteurs pour l'interface et l'estimation de taille d'archive."""
    with _connect() as con:
        stats = {
            "documents": con.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "runs": con.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "fiches": con.execute(
                "SELECT COUNT(*) FROM fiches WHERE status = 'ok'"
            ).fetchone()[0],
            "ocr_bytes": con.execute(
                "SELECT COALESCE(SUM(LENGTH(payload_gz)), 0) FROM ocr_cache"
            ).fetchone()[0],
        }
    chemin = Path(_DB_PATH)
    stats["db_bytes"] = chemin.stat().st_size if chemin.exists() else 0
    return stats


# --- Export / import ---------------------------------------------------------


def export_bundle(include_ocr=True, mistralai_version=None):
    """
    Archive ZIP de JSON de tout l'historique.

    Format volontairement inerte : jamais un fichier .sqlite. Un fichier SQLite
    est du schéma AUTANT que des données (déclencheurs, vues), et l'ouvrir avec
    le moteur est le seul moyen de l'inspecter — inacceptable pour un import
    anonyme sur une application publique partagée. Aucune clé primaire n'est
    exportée : tout est référencé par clé naturelle (sha256, uid, seq).

    Les charges OCR sont incluses par défaut : c'est le seul coût non
    reproductible. Sans elles l'archive conserve des résultats ; avec elles,
    elle rend la capacité de travailler.
    """
    with _connect() as con:
        documents = _rows(
            con.execute(
                "SELECT sha256, filename, size_bytes, page_count, created_at "
                "FROM documents ORDER BY id"
            )
        )
        runs = _rows(
            con.execute(
                """
                SELECT r.uid, d.sha256 AS document_sha256, r.status, r.mode,
                       r.started_at, r.finished_at, r.model_ocr, r.model_segmentation,
                       r.model_extraction, r.prompt_extraction_sha256,
                       r.prompt_segmentation_sha256, r.schema_rex_sha256,
                       r.schema_list_sha256, r.segmentation_json, r.prompt_tokens,
                       r.cached_tokens, r.completion_tokens, r.error
                  FROM runs r JOIN documents d ON d.id = r.document_id
                 ORDER BY r.id
                """
            )
        )
        fiches = _rows(
            con.execute(
                """
                SELECT r.uid AS run_uid, f.seq, f.titre, f.page_debut, f.page_fin,
                       f.status, f.data_json, f.error, f.categorie,
                       f.model_extraction, f.prompt_hash, f.prompt_tokens,
                       f.cached_tokens, f.completion_tokens
                  FROM fiches f JOIN runs r ON r.id = f.run_id
                 ORDER BY f.run_id, f.seq
                """
            )
        )
        charges = []
        if include_ocr:
            charges = _rows(
                con.execute(
                    "SELECT d.sha256, o.payload_gz FROM ocr_cache o "
                    "JOIN documents d ON d.id = o.document_id "
                    "WHERE o.invalid_reason IS NULL ORDER BY d.id"
                )
            )

    manifest = {
        "format": FORMAT_ARCHIVE,
        "version": SCHEMA_VERSION,
        "exported_at": _maintenant(),
        "mistralai_version": mistralai_version,
        "includes_ocr": bool(include_ocr),
        "counts": {
            "documents": len(documents),
            "runs": len(runs),
            "fiches": len(fiches),
            "ocr": len(charges),
        },
    }

    tampon = BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _json(manifest))
        zf.writestr("documents.json", _json(documents))
        zf.writestr("runs.json", _json(runs))
        zf.writestr("fiches.json", _json(fiches))
        for charge in charges:
            # Déjà gzippé : ZIP_STORED, recompresser ne gagnerait rien.
            zf.writestr(
                f"ocr/{charge['sha256']}.json.gz",
                charge["payload_gz"],
                zipfile.ZIP_STORED,
            )
    return tampon.getvalue()


def import_bundle(archive_bytes, on_conflict="ignorer"):
    """
    Réimporte une archive produite par export_bundle. Renvoie un rapport de
    comptages. Lève BundleInvalide si un contrôle échoue — et dans ce cas la
    base est laissée intacte : tout l'import tient dans une transaction.

    `on_conflict` : « ignorer » saute un run déjà présent (et ses fiches) ;
    « remplacer » supprime le run local et le réinsère.

    Risque résiduel assumé et non corrigeable ici : le sha256 est celui du PDF,
    que l'archive ne contient pas. Une charge OCR importée ne peut donc pas être
    vérifiée contre le hash qu'elle revendique — une archive hostile peut
    empoisonner le cache OCR. C'est pourquoi les charges locales existantes ne
    sont JAMAIS écrasées, et pourquoi ce bouton est le premier à protéger si un
    mot de passe est ajouté un jour.
    """
    if on_conflict not in ("ignorer", "remplacer"):
        raise ValueError(f"on_conflict inconnu : {on_conflict!r}")
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise BundleInvalide(
            f"archive trop volumineuse ({len(archive_bytes) / 1e6:.1f} Mo, "
            f"maximum {MAX_ARCHIVE_BYTES / 1e6:.0f} Mo)"
        )

    try:
        zf = zipfile.ZipFile(BytesIO(archive_bytes))
    except zipfile.BadZipFile as err:
        raise BundleInvalide(f"archive ZIP illisible : {err}") from err

    with zf:
        infos = zf.infolist()
        # Liste blanche des membres : c'est aussi la défense contre la traversée
        # de chemin. On ne fait jamais extract() sur le disque, seulement read().
        for info in infos:
            if info.filename not in _MEMBRES_ATTENDUS and not _NOM_MEMBRE_OCR.match(
                info.filename
            ):
                raise BundleInvalide(f"membre inattendu : {info.filename!r}")
        total_declare = sum(i.file_size for i in infos)
        if total_declare > MAX_DECOMPRESSE_BYTES:
            raise BundleInvalide(
                f"contenu décompressé trop volumineux ({total_declare / 1e6:.0f} Mo)"
            )
        noms = {i.filename for i in infos}
        manquants = _MEMBRES_ATTENDUS - noms
        if manquants:
            raise BundleInvalide(f"membres absents : {sorted(manquants)}")

        manifest = _lire_json_membre(zf, "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_ARCHIVE:
            raise BundleInvalide("ce n'est pas une archive d'historique REX-MH")
        version = manifest.get("version")
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise BundleInvalide(
                f"version d'archive non prise en charge : {version!r} "
                f"(cette application lit jusqu'à {SCHEMA_VERSION})"
            )

        documents = _lire_liste_membre(zf, "documents.json")
        runs = _lire_liste_membre(zf, "runs.json")
        fiches = _lire_liste_membre(zf, "fiches.json")
        if len(documents) > MAX_DOCUMENTS:
            raise BundleInvalide(f"trop de documents ({len(documents)})")
        if len(fiches) > MAX_FICHES:
            raise BundleInvalide(f"trop de fiches ({len(fiches)})")

        for doc in documents:
            if not _SHA256.match(str(doc.get("sha256", ""))):
                raise BundleInvalide(f"sha256 invalide : {doc.get('sha256')!r}")
        for run in runs:
            if run.get("status") not in _STATUTS_RUN:
                raise BundleInvalide(f"statut de run invalide : {run.get('status')!r}")
            if run.get("mode") not in _MODES:
                raise BundleInvalide(f"mode invalide : {run.get('mode')!r}")
            if not run.get("uid"):
                raise BundleInvalide("run sans uid")
        for fiche in fiches:
            if fiche.get("status") not in _STATUTS_FICHE:
                raise BundleInvalide(
                    f"statut de fiche invalide : {fiche.get('status')!r}"
                )

        charges_ocr = {}
        for info in infos:
            if _NOM_MEMBRE_OCR.match(info.filename):
                sha = info.filename[len("ocr/") : -len(".json.gz")]
                charges_ocr[sha] = zf.read(info.filename)

    rapport = {
        "documents_ajoutes": 0,
        "documents_existants": 0,
        "ocr_ajoutes": 0,
        "ocr_ignores": 0,
        "runs_ajoutes": 0,
        "runs_ignores": 0,
        "runs_remplaces": 0,
        "fiches_ajoutees": 0,
    }
    fiches_par_run = {}
    for fiche in fiches:
        fiches_par_run.setdefault(fiche.get("run_uid"), []).append(fiche)

    with _connect(write=True) as con:
        ids_par_sha = {}
        for doc in documents:
            sha = doc["sha256"]
            existant = con.execute(
                "SELECT id FROM documents WHERE sha256 = ?", (sha,)
            ).fetchone()
            if existant:
                ids_par_sha[sha] = existant[0]
                rapport["documents_existants"] += 1
            else:
                cur = con.execute(
                    "INSERT INTO documents (sha256, filename, size_bytes, page_count, "
                    "created_at) VALUES (?, ?, ?, ?, COALESCE(?, "
                    "strftime('%Y-%m-%dT%H:%M:%SZ','now')))",
                    (
                        sha,
                        doc.get("filename") or sha[:12],
                        doc.get("size_bytes"),
                        doc.get("page_count"),
                        doc.get("created_at"),
                    ),
                )
                ids_par_sha[sha] = cur.lastrowid
                rapport["documents_ajoutes"] += 1

        for sha, payload_gz in charges_ocr.items():
            document_id = ids_par_sha.get(sha)
            if document_id is None:
                continue
            deja = con.execute(
                "SELECT 1 FROM ocr_cache WHERE document_id = ?", (document_id,)
            ).fetchone()
            if deja:
                # Jamais écraser : la charge locale a été produite par le SDK
                # local et est déjà de confiance.
                rapport["ocr_ignores"] += 1
                continue
            try:
                clair = gzip.decompress(payload_gz)
                json.loads(clair)
            except Exception as err:
                raise BundleInvalide(
                    f"charge OCR illisible pour {sha[:12]} : {err}"
                ) from err
            con.execute(
                "INSERT INTO ocr_cache (document_id, cle_ocr, payload_gz, "
                "payload_bytes, sdk_version) VALUES (?, ?, ?, ?, ?)",
                (document_id, "importe", payload_gz, len(clair), "importe"),
            )
            rapport["ocr_ajoutes"] += 1

        for run in runs:
            document_id = ids_par_sha.get(run.get("document_sha256"))
            if document_id is None:
                continue
            existant = con.execute(
                "SELECT id FROM runs WHERE uid = ?", (run["uid"],)
            ).fetchone()
            if existant:
                if on_conflict == "ignorer":
                    rapport["runs_ignores"] += 1
                    continue
                con.execute("DELETE FROM runs WHERE id = ?", (existant[0],))
                rapport["runs_remplaces"] += 1
            cur = con.execute(
                """
                INSERT INTO runs (uid, document_id, status, mode, started_at,
                                  finished_at, model_ocr, model_segmentation,
                                  model_extraction, prompt_extraction_sha256,
                                  prompt_segmentation_sha256, schema_rex_sha256,
                                  schema_list_sha256, segmentation_json,
                                  prompt_tokens, cached_tokens, completion_tokens,
                                  error)
                VALUES (?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["uid"],
                    document_id,
                    run["status"],
                    run["mode"],
                    run.get("started_at"),
                    run.get("finished_at"),
                    run.get("model_ocr"),
                    run.get("model_segmentation"),
                    run.get("model_extraction"),
                    run.get("prompt_extraction_sha256"),
                    run.get("prompt_segmentation_sha256"),
                    run.get("schema_rex_sha256"),
                    run.get("schema_list_sha256"),
                    run.get("segmentation_json"),
                    run.get("prompt_tokens") or 0,
                    run.get("cached_tokens") or 0,
                    run.get("completion_tokens") or 0,
                    run.get("error"),
                ),
            )
            run_id = cur.lastrowid
            if not existant:
                rapport["runs_ajoutes"] += 1
            for fiche in fiches_par_run.get(run["uid"], []):
                con.execute(
                    """
                    INSERT INTO fiches (run_id, document_id, seq, titre, page_debut,
                                        page_fin, status, data_json, error, categorie,
                                        model_extraction, prompt_hash, prompt_tokens,
                                        cached_tokens, completion_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, seq) DO NOTHING
                    """,
                    (
                        run_id,
                        document_id,
                        fiche.get("seq"),
                        fiche.get("titre"),
                        fiche.get("page_debut"),
                        fiche.get("page_fin"),
                        fiche["status"],
                        fiche.get("data_json"),
                        fiche.get("error"),
                        fiche.get("categorie"),
                        fiche.get("model_extraction"),
                        fiche.get("prompt_hash"),
                        fiche.get("prompt_tokens"),
                        fiche.get("cached_tokens"),
                        fiche.get("completion_tokens"),
                    ),
                )
                rapport["fiches_ajoutees"] += 1

    return rapport


def _lire_json_membre(zf, nom, taille_max=8 * 1024 * 1024):
    """Lit un membre en bornant la taille RÉELLE lue, pas celle déclarée."""
    with zf.open(nom) as flux:
        brut = flux.read(taille_max + 1)
    if len(brut) > taille_max:
        raise BundleInvalide(f"membre {nom!r} trop volumineux")
    try:
        return json.loads(brut)
    except json.JSONDecodeError as err:
        raise BundleInvalide(f"membre {nom!r} : JSON invalide ({err})") from err


def _lire_liste_membre(zf, nom):
    valeur = _lire_json_membre(zf, nom, taille_max=64 * 1024 * 1024)
    if not isinstance(valeur, list):
        raise BundleInvalide(f"membre {nom!r} : liste attendue")
    if not all(isinstance(x, dict) for x in valeur):
        raise BundleInvalide(f"membre {nom!r} : objets attendus")
    return valeur


def _json(valeur):
    return json.dumps(valeur, ensure_ascii=False, indent=1)


def _maintenant():
    with _connect() as con:
        return con.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        ).fetchone()[0]
