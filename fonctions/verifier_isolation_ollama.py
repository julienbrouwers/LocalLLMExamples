"""Audite et vérifie l'isolation réseau d'Ollama pendant une inférence locale réelle.

Produit un résultat explicite par contrôle parmi :
- `reussi`        : contrôle exécuté et conforme
- `echec`         : contrôle exécuté et non conforme
- `non_execute`   : contrôle non exécuté (plateforme, prérequis ou capture absente)
- `non_concluant` : contrôle exécuté mais preuve insuffisante (ex. fichier log absent)

Enregistre aussi les versions et digests (Ollama `/api/version`, modèle `/api/tags`),
les temps (`total_duration`, `load_duration`), la colonne `PROCESSOR` de `ollama ps`,
la fenêtre et le nombre d'échantillons TCP, les fichiers inspectés et les limites de mesure.

Processus surveillés : `ollama*` (serveur + application Desktop) et `llama-server`
(moteur d'inférence d'Ollama 0.40.x, qui charge les poids et traite les prompts).

Le durcissement est vérifié côté SERVEUR (ligne `server config` / `Ollama cloud disabled`
du journal du serveur actif), pas dans l'environnement du shell qui lance ce script.
Avec `ollama serve`, redirigez son journal et passez-le via `--journal-serveur`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from commun import OLLAMA_URL, appeler_ollama, decharger_modele, extraire_mesures


ADRESSE_LOOPBACK = {"127.0.0.1", "::1", "0.0.0.0", "::", ""}
REGLE_CLI = "Ollama-Block-Outbound-LocalOnly"
REGLE_APP = "Ollama-Block-Outbound-App-LocalOnly"
REGLE_RUNNER = "Ollama-Block-Outbound-LlamaServer"
INTERVALLE_POLLING_S = 0.35
TAILLE_LECTURE_CONFIG = 1_048_576
SEUIL_CHARGEMENT_FROID_S = 0.5


def _executer_powershell(commande: str) -> tuple[bool, str, str]:
    """Exécute une commande PowerShell en lecture seule et renvoie `(ok, stdout, erreur)`."""
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", commande],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", f"erreur d'exécution PowerShell : {exc}"
    if res.returncode != 0:
        err = res.stderr.strip() or f"code de sortie PowerShell {res.returncode}"
        return False, res.stdout.strip(), err
    return True, res.stdout.strip(), ""


def _lire_json_api(endpoint: str, timeout: int = 10) -> dict[str, Any]:
    """Lit un endpoint GET JSON de l'API locale Ollama."""
    try:
        with urlopen(f"{OLLAMA_URL}/api/{endpoint}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return {}


def collecter_versions_et_digest(modele: str) -> dict[str, Any]:
    """Collecte la version Ollama (`/api/version`) et le digest exact du modèle (`/api/tags`)."""
    ver = _lire_json_api("version")
    tags = _lire_json_api("tags")
    digest = None
    for item in tags.get("models", []):
        if item.get("name") == modele or item.get("model") == modele:
            digest = item.get("digest")
            break
    return {
        "ollama_api_version": ver.get("version"),
        "modele": modele,
        "modele_digest": digest,
        "python": platform.python_version(),
        "os": platform.platform(),
    }


def lire_ollama_ps(modele: str) -> dict[str, Any]:
    """Lit `ollama ps` et extrait la ligne correspondant au modèle (dont PROCESSOR)."""
    try:
        res = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10)
        lignes = [l for l in res.stdout.splitlines() if l.strip()]
    except (OSError, subprocess.SubprocessError):
        return {"brut": [], "ligne_modele": None}

    ligne_modele = next((l for l in lignes[1:] if l.startswith(modele)), None)
    return {"brut": lignes, "ligne_modele": ligne_modele}


def inspecter_sockets_ollama() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Retourne `(ecoutes, connexions_distantes, erreur)` pour `ollama*` et `llama-server`.

    Chaque connexion porte le nom du processus et son heure de création (`Creation`, ISO 8601),
    ce qui permet de distinguer une connexion ouverte AVANT le test d'une connexion ouverte PENDANT.
    """
    if platform.system() != "Windows":
        return [], [], None

    ps = (
        "$procs = @(Get-Process -Name 'ollama*','llama-server' -ErrorAction SilentlyContinue); "
        "if (-not $procs) { '[]'; exit 0 } "
        "$noms = @{}; foreach ($x in $procs) { $noms[[int]$x.Id] = $x.ProcessName }; "
        "$c = @(Get-NetTCPConnection -OwningProcess $procs.Id -ErrorAction SilentlyContinue | "
        "Select-Object OwningProcess, @{n='Processus';e={$noms[[int]$_.OwningProcess]}}, "
        "@{n='State';e={[string]$_.State}}, LocalAddress, LocalPort, RemoteAddress, RemotePort, "
        "@{n='Creation';e={ if ($_.CreationTime) { $_.CreationTime.ToString('yyyy-MM-ddTHH:mm:ss.ffffffzzz') } }}); "
        "ConvertTo-Json -InputObject $c -Compress"
    )
    ok, sortie, err = _executer_powershell(ps)
    if not ok:
        return [], [], f"collecte sockets impossible : {err}"
    if not sortie:
        return [], [], None
    try:
        donnees = json.loads(sortie)
    except ValueError as exc:
        return [], [], f"parsing JSON sockets impossible : {exc}"
    if isinstance(donnees, dict):
        donnees = [donnees]

    ecoutes: list[dict[str, Any]] = []
    distantes: list[dict[str, Any]] = []
    for item in donnees:
        etat = str(item.get("State", ""))
        remote = str(item.get("RemoteAddress", ""))
        if etat in ("2", "Listen"):
            ecoutes.append(item)
        elif remote not in ADRESSE_LOOPBACK:
            distantes.append(item)
    return ecoutes, distantes, None


def _horodatage(texte: str | None) -> dt.datetime | None:
    """Parse un horodatage ISO 8601 (PowerShell ou journal Ollama) ; None si illisible."""
    if not texte:
        return None
    try:
        return dt.datetime.fromisoformat(texte)
    except ValueError:
        return None


def classer_connexions(
    connexions: list[dict[str, Any]], debut_test: dt.datetime
) -> tuple[list[str], list[str]]:
    """Déduplique et sépare les connexions distantes en `(anterieures_au_test, pendant_le_test)`.

    Une connexion sans heure de création est classée « pendant » (hypothèse prudente).
    """
    vues: set[tuple[Any, ...]] = set()
    anterieures: list[str] = []
    pendant: list[str] = []
    for c in connexions:
        cle = (c.get("OwningProcess"), c.get("LocalPort"), c.get("RemoteAddress"), c.get("RemotePort"))
        if cle in vues:
            continue
        vues.add(cle)
        creation = _horodatage(c.get("Creation"))
        libelle = (
            f"{c.get('Processus') or c.get('OwningProcess')} -> {c.get('RemoteAddress')}:{c.get('RemotePort')}"
            f" (créée {creation.strftime('%H:%M:%S') if creation else 'à une heure inconnue'})"
        )
        if creation is not None and creation < debut_test:
            anterieures.append(libelle)
        else:
            pendant.append(libelle)
    return anterieures, pendant


def regles_pare_feu_attendues() -> tuple[str, ...]:
    """Règles exigées : CLI + Desktop, plus le moteur `llama-server.exe` s'il est installé."""
    regles = [REGLE_CLI, REGLE_APP]
    exe = shutil.which("ollama")
    if exe and (Path(exe).resolve().parent / "lib" / "ollama" / "llama-server.exe").exists():
        regles.append(REGLE_RUNNER)
    return tuple(regles)


def verifier_pare_feu_windows() -> dict[str, Any]:
    """Exige TOUTES les règles sortantes bloquantes attendues, effectives dans ActiveStore, pour PASS."""
    if platform.system() != "Windows":
        return {"etat": "non_execute", "detail": "vérification pare-feu automatique dédiée à Windows"}

    attendues = regles_pare_feu_attendues()
    noms_ps = ", ".join(f"'{n}'" for n in attendues)
    ps = (
        f"$r = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name {noms_ps} -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Direction -eq 'Outbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True' } | "
        "Select-Object -ExpandProperty Name); "
        "$r | ConvertTo-Json -Compress"
    )
    ok, sortie, err = _executer_powershell(ps)
    if not ok:
        return {"etat": "non_concluant", "detail": f"collecte pare-feu ActiveStore impossible : {err}"}
    if not sortie:
        regles: list[str] = []
    else:
        try:
            brut = json.loads(sortie)
            regles = [brut] if isinstance(brut, str) else list(brut)
        except ValueError as exc:
            return {"etat": "non_concluant", "detail": f"parsing JSON pare-feu impossible : {exc}"}

    actives = set(regles) & set(attendues)
    manquantes = [r for r in attendues if r not in actives]
    n = len(attendues)
    if not manquantes:
        return {"etat": "reussi", "detail": f"{n}/{n} règles effectives actives (ActiveStore) : {', '.join(attendues)}"}
    if actives:
        return {
            "etat": "echec",
            "detail": f"règles incomplètes ({len(actives)}/{n} actives) : manquante(s) = {', '.join(manquantes)}",
        }
    return {"etat": "echec", "detail": f"0/{n} règles sortantes Block actives dans ActiveStore"}


def chemin_journal_serveur(chemin_explicite: str) -> Path | None:
    """Journal du serveur actif : `--journal-serveur`, sinon `%LOCALAPPDATA%\\Ollama\\server.log` (Desktop)."""
    if chemin_explicite:
        return Path(chemin_explicite)
    local_app = os.environ.get("LOCALAPPDATA", "")
    return Path(local_app) / "Ollama" / "server.log" if local_app else None


def lire_texte_journal(chemin: Path, depuis: int = 0, limite: int | None = None) -> str:
    """Lit un journal à partir de l'octet `depuis` (ou ses `limite` derniers octets).

    Gère l'UTF-8 (journal Ollama Desktop, `Start-Process -RedirectStandardError`) et l'UTF-16 LE
    avec BOM (redirection `2>` de Windows PowerShell 5.1).
    """
    with chemin.open("rb") as f:
        entete = f.read(2)
        utf16 = entete == b"\xff\xfe"
        taille = chemin.stat().st_size
        if limite is not None and taille - depuis > limite:
            depuis = taille - limite
        if utf16:
            depuis = max(2, depuis - (depuis % 2))
        f.seek(depuis)
        brut = f.read()
    return brut.decode("utf-16-le" if utf16 else "utf-8", errors="ignore")


def demarrage_serveur_actif() -> tuple[dt.datetime | None, str | None]:
    """Heure de démarrage du processus qui écoute sur le port de `OLLAMA_URL` (Windows)."""
    if platform.system() != "Windows":
        return None, "heure de démarrage du serveur lisible uniquement sous Windows"
    port = urlparse(OLLAMA_URL).port or 11434
    ps = (
        f"$l = Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if (-not $l) { exit 0 } "
        "(Get-Process -Id $l.OwningProcess).StartTime.ToString('yyyy-MM-ddTHH:mm:ss.ffffffzzz')"
    )
    ok, sortie, err = _executer_powershell(ps)
    if not ok:
        return None, f"lecture du processus serveur impossible : {err}"
    if not sortie:
        return None, f"aucun processus n'écoute sur le port {port}"
    return _horodatage(sortie), None


def lire_config_serveur(journal: Path | None) -> dict[str, Any]:
    """Extrait la configuration EFFECTIVE du serveur actif depuis son journal.

    Retourne `valide=True` seulement si la ligne `Ollama cloud disabled` est postérieure au
    démarrage du processus qui écoute actuellement (sinon le journal décrit un ancien serveur).
    """
    resultat: dict[str, Any] = {"valide": False, "journal": str(journal) if journal else None}
    if journal is None or not journal.exists():
        resultat["raison"] = f"journal du serveur introuvable ({journal})"
        return resultat
    try:
        texte = lire_texte_journal(journal, limite=TAILLE_LECTURE_CONFIG)
    except OSError as exc:
        resultat["raison"] = f"lecture impossible de {journal.name} : {exc}"
        return resultat

    cloud = list(re.finditer(r'time=(\S+) .*?msg="Ollama cloud disabled: (true|false)"', texte))
    if not cloud:
        resultat["raison"] = f"aucune ligne 'Ollama cloud disabled' dans {journal.name}"
        return resultat
    dernier = cloud[-1]
    heure_config = _horodatage(dernier.group(1))
    config = list(re.finditer(r'msg="server config" env="map\[(.*?)\]"\s*$', texte, re.MULTILINE))
    env_brut = config[-1].group(1) if config else ""
    host = re.search(r"OLLAMA_HOST:(\S+)", env_brut)
    origins = re.search(r"OLLAMA_ORIGINS:\[([^\]]*)\]", env_brut)
    resultat.update(
        {
            "cloud_desactive": dernier.group(2) == "true",
            "heure_config": heure_config.isoformat() if heure_config else None,
            "ollama_host": host.group(1) if host else None,
            "origines": origins.group(1).split() if origins else [],
        }
    )

    demarrage, err = demarrage_serveur_actif()
    if demarrage is None or heure_config is None:
        resultat["raison"] = err or "horodatage de configuration illisible"
        return resultat
    if heure_config < demarrage - dt.timedelta(seconds=5):
        resultat["raison"] = (
            f"la dernière configuration de {journal.name} ({heure_config:%H:%M:%S}) précède le démarrage "
            f"du serveur actif ({demarrage:%H:%M:%S}) : ce journal n'est pas celui du serveur qui répond"
        )
        return resultat
    resultat["valide"] = True
    return resultat


def verifier_durcissement_serveur(config: dict[str, Any]) -> dict[str, Any]:
    """Vérifie le durcissement EFFECTIF du serveur actif (et non l'environnement de ce shell)."""
    if not config.get("valide"):
        return {
            "etat": "non_concluant",
            "detail": f"configuration du serveur actif non vérifiable : {config.get('raison')} "
            "(avec 'ollama serve', redirigez son journal et passez --journal-serveur)",
        }
    host = (config.get("ollama_host") or "").removeprefix("http://").removeprefix("https://").split("/")[0]
    host_only = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
    if config.get("origines") and "*" in config["origines"]:
        return {"etat": "echec", "detail": "DANGER : le serveur actif accepte OLLAMA_ORIGINS='*' (toute page web peut l'interroger)"}
    if host_only and host_only not in ("127.0.0.1", "localhost", "::1"):
        return {"etat": "echec", "detail": f"serveur actif exposé hors loopback : OLLAMA_HOST={config.get('ollama_host')}"}
    if not config.get("cloud_desactive"):
        return {
            "etat": "echec",
            "detail": "serveur actif démarré sans OLLAMA_NO_CLOUD (journal : 'Ollama cloud disabled: false') ; "
            "définissez les variables puis REDÉMARREZ le serveur",
        }
    return {
        "etat": "reussi",
        "detail": f"confirmé côté serveur ({Path(config['journal']).name}, config de {config.get('heure_config', '?')[11:19]}) : "
        f"cloud disabled: true, OLLAMA_HOST={config.get('ollama_host')}",
    }


def lire_cache_dns_windows() -> tuple[set[str], str | None]:
    """Retourne `(entrees, erreur)` pour les noms de domaines présents dans le cache DNS Windows."""
    if platform.system() != "Windows":
        return set(), None
    ps = (
        "@(Get-DnsClientCache -ErrorAction Stop | "
        "Select-Object -ExpandProperty Entry -Unique) | ConvertTo-Json -Compress"
    )
    ok, sortie, err = _executer_powershell(ps)
    if not ok:
        return set(), f"collecte cache DNS impossible : {err}"
    if not sortie:
        return set(), None
    try:
        brut = json.loads(sortie)
        if isinstance(brut, str):
            return {brut.lower()}, None
        return {str(x).lower() for x in brut}, None
    except ValueError as exc:
        return set(), f"parsing JSON cache DNS impossible : {exc}"


def taille_journal(chemin: Path | None) -> int | None:
    """Taille actuelle du journal (None s'il est absent)."""
    try:
        return chemin.stat().st_size if chemin is not None and chemin.exists() else None
    except OSError:
        return None


def verifier_canari_journal(
    jeton: str, journal: Path | None, taille_avant: int | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Cherche le jeton dans les octets écrits dans le journal du serveur PENDANT le test.

    Si rien n'a été écrit pendant le test, le fichier n'est pas le journal du serveur actif
    (cas typique : `ollama serve` journalise sur la console) et le contrôle est `non_concluant`.
    """
    meta: dict[str, Any] = {"chemin": str(journal) if journal else None, "present": False, "octets_ecrits_pendant_test": 0}
    if journal is None:
        return {"etat": "non_execute", "detail": "LOCALAPPDATA absent et --journal-serveur non fourni"}, meta
    if not journal.exists():
        return {"etat": "non_concluant", "detail": f"{journal} absent à cet emplacement"}, meta

    try:
        taille_apres = journal.stat().st_size
        meta["present"] = True
        depuis = taille_avant if taille_avant is not None and taille_avant <= taille_apres else 0
        meta["octets_ecrits_pendant_test"] = taille_apres - depuis
        if taille_apres == depuis:
            return {
                "etat": "non_concluant",
                "detail": f"aucune ligne écrite dans {journal.name} pendant le test : ce n'est pas le journal du "
                "serveur actif ('ollama serve' journalise sur la console -> --journal-serveur)",
            }, meta
        extrait = lire_texte_journal(journal, depuis=depuis)
        if jeton in extrait:
            return {"etat": "echec", "detail": f"jeton '{jeton}' détecté en clair dans {journal.name}"}, meta
        return {
            "etat": "reussi",
            "detail": f"0 occurrence du marqueur dans les {meta['octets_ecrits_pendant_test']} octets écrits "
            f"dans {journal.name} pendant le test",
        }, meta
    except OSError as exc:
        return {"etat": "non_concluant", "detail": f"lecture impossible de {journal.name} : {exc}"}, meta


def construire_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modele", default="qwen2.5:3b", help="Modèle local déjà installé (par défaut : qwen2.5:3b)")
    parser.add_argument(
        "--canari",
        default="",
        help="Marqueur canari explicite (par défaut : généré de façon unique par exécution via UUID)",
    )
    parser.add_argument(
        "--keep-alive",
        default="0",
        help="Durée keep_alive transmise à Ollama (par défaut : 0 ; utiliser '5m' pour comparer froid vs chaud)",
    )
    parser.add_argument(
        "--journal-serveur",
        default="",
        help="Journal du serveur Ollama actif (par défaut : %%LOCALAPPDATA%%\\Ollama\\server.log d'Ollama Desktop ; "
        "avec 'ollama serve', redirigez stderr vers un fichier et passez son chemin)",
    )
    parser.add_argument(
        "--capture-pcap",
        default="",
        help="Chemin optionnel vers une capture PktMon .pcapng associée à cette exécution",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exige la réussite des contrôles locaux + durcissement + pare-feu pour retourner exit 0",
    )
    parser.add_argument("--json", action="store_true", help="Affiche le rapport structuré complet en JSON")
    return parser


def main() -> None:
    parser = construire_parser()
    args = parser.parse_args()

    est_windows = platform.system() == "Windows"
    jeton_canari = args.canari.strip() or f"CANARI-CONFIDENTIEL-LOCAL-{uuid.uuid4().hex}"
    horodatage = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    versions = collecter_versions_et_digest(args.modele)
    journal = chemin_journal_serveur(args.journal_serveur)

    debut_test = dt.datetime.now().astimezone()
    taille_journal_avant = taille_journal(journal)
    ecoutes_avant, distantes_avant, err_sock_avant = inspecter_sockets_ollama()
    dns_avant, err_dns_avant = lire_cache_dns_windows()

    # 1. Binding strict loopback
    if not est_windows:
        ctrl_binding = {"etat": "non_execute", "detail": "inspection Get-NetTCPConnection dédiée à Windows"}
    elif err_sock_avant:
        ctrl_binding = {"etat": "non_concluant", "detail": err_sock_avant}
    else:
        exposees = [
            f"{e.get('LocalAddress')}:{e.get('LocalPort')}"
            for e in ecoutes_avant
            if str(e.get("LocalAddress")) not in ("127.0.0.1", "::1")
        ]
        if not ecoutes_avant:
            ctrl_binding = {"etat": "non_concluant", "detail": "aucun socket Listen détecté pour ollama* / llama-server"}
        elif exposees:
            ctrl_binding = {"etat": "echec", "detail": f"socket(s) exposé(s) hors loopback : {', '.join(exposees)}"}
        else:
            detail = ", ".join(f"{e.get('LocalAddress')}:{e.get('LocalPort')}" for e in ecoutes_avant)
            ctrl_binding = {"etat": "reussi", "detail": detail}

    # 2. Inférence locale sur le marqueur canari + surveillance TCP continue
    distantes_pendant: list[dict[str, Any]] = []
    erreurs_sock_pendant: list[str] = []
    nb_echantillons = 0
    stop_event = threading.Event()

    def _surveiller_sockets() -> None:
        nonlocal nb_echantillons
        while not stop_event.is_set():
            _, dist, err_s = inspecter_sockets_ollama()
            nb_echantillons += 1
            if err_s:
                erreurs_sock_pendant.append(err_s)
            if dist:
                distantes_pendant.extend(dist)
            stop_event.wait(INTERVALLE_POLLING_S)

    prompt_canari = f"Copy exactly the following marker and nothing else: {jeton_canari}"
    keep_alive_val: Any = 0 if args.keep_alive == "0" else args.keep_alive
    donnees = {
        "model": args.modele,
        "stream": False,
        "keep_alive": keep_alive_val,
        "options": {"temperature": 0, "seed": 42, "num_ctx": 2048, "num_predict": 64},
        "messages": [{"role": "user", "content": prompt_canari}],
    }

    t0 = time.monotonic()
    watcher = threading.Thread(target=_surveiller_sockets, daemon=True)
    if est_windows:
        watcher.start()

    mesures_inf: dict[str, Any] = {
        "total_s": None,
        "chargement_s": None,
        "prompt_tokens": None,
        "eval_tokens": None,
        "keep_alive": keep_alive_val,
    }
    try:
        reponse = appeler_ollama("chat", donnees, timeout=120)
        mesures = extraire_mesures(reponse)
        texte_reponse = reponse["message"]["content"].strip()
        total_s = round(reponse.get("total_duration", 0) / 1e9, 2)
        load_s = round(reponse.get("load_duration", 0) / 1e9, 2)
        mesures_inf.update(
            {
                "total_s": total_s,
                "chargement_s": load_s,
                "prompt_tokens": mesures.get("prompt_eval_count"),
                "eval_tokens": mesures.get("eval_count"),
            }
        )
        echo_exact = texte_reponse == jeton_canari
        mode = "FROID, modèle chargé pendant l'appel" if load_s > SEUIL_CHARGEMENT_FROID_S else "CHAUD, modèle déjà en mémoire"
        mesures_inf["mode"] = "froid" if load_s > SEUIL_CHARGEMENT_FROID_S else "chaud"
        if echo_exact:
            ctrl_canari = {
                "etat": "reussi",
                "detail": f"écho exact en {total_s}s [{mode}] (chargement={load_s}s, {mesures.get('prompt_eval_count')} -> {mesures.get('eval_count')} tok)",
            }
        else:
            ctrl_canari = {
                "etat": "echec",
                "detail": f"écho inexact (attendu='{jeton_canari}', obtenu='{texte_reponse}') en {total_s}s",
            }
    except (HTTPError, URLError, TimeoutError) as erreur:
        decharger_modele(args.modele)
        ctrl_canari = {"etat": "echec", "detail": f"échec appel API locale : {erreur}"}
    finally:
        stop_event.set()
        if est_windows:
            watcher.join(timeout=5)
    fenetre_s = round(time.monotonic() - t0, 2)

    ollama_ps = lire_ollama_ps(args.modele)
    _, distantes_apres, err_sock_apres = inspecter_sockets_ollama()
    dns_apres, err_dns_apres = lire_cache_dns_windows()

    # 3. Connexions TCP distantes observées
    if not est_windows:
        ctrl_tcp = {"etat": "non_execute", "detail": "surveillance TCP dédiée à Windows"}
    else:
        toutes_distantes = distantes_avant + distantes_pendant + distantes_apres
        anterieures, ouvertes_pendant = classer_connexions(toutes_distantes, debut_test)
        erreurs_sock = [e for e in (err_sock_avant, *erreurs_sock_pendant, err_sock_apres) if e]
        if ouvertes_pendant:
            ctrl_tcp = {
                "etat": "echec",
                "detail": f"{len(ouvertes_pendant)} connexion(s) distante(s) ouverte(s) pendant le test : {'; '.join(ouvertes_pendant)}",
            }
        elif anterieures:
            ctrl_tcp = {
                "etat": "non_concluant",
                "detail": f"{len(anterieures)} connexion(s) distante(s) ouverte(s) AVANT le test, non imputables à "
                f"l'inférence (ex. vérification de mises à jour / recommandations vers ollama.com) : {'; '.join(anterieures)}. "
                "Relancez l'audit une fois fermées (~2 min après le démarrage d'Ollama) ou activez le pare-feu",
            }
        elif erreurs_sock:
            ctrl_tcp = {
                "etat": "non_concluant",
                "detail": f"erreur(s) de collecte sockets ({len(erreurs_sock)}) : {erreurs_sock[0]}",
            }
        else:
            ctrl_tcp = {
                "etat": "reussi",
                "detail": f"0 connexion TCP distante sur ollama* + llama-server ({nb_echantillons} échantillon(s) sur {fenetre_s}s, "
                f"soit ~{round(fenetre_s / nb_echantillons, 1) if nb_echantillons else '?'}s entre deux relevés)",
            }

    # 4. Cache DNS Windows
    if not est_windows:
        ctrl_dns = {"etat": "non_execute", "detail": "cache DNS dédié à Windows"}
    elif err_dns_avant or err_dns_apres:
        ctrl_dns = {
            "etat": "non_concluant",
            "detail": err_dns_avant or err_dns_apres or "erreur de collecte cache DNS",
        }
    else:
        nouveaux_dns = sorted(dns_apres - dns_avant)
        dns_suspects = [d for d in nouveaux_dns if "ollama" in d or jeton_canari.lower() in d]
        if not dns_suspects:
            ctrl_dns = {
                "etat": "reussi",
                "detail": f"0 nouvelle entrée DNS liée à Ollama ou au marqueur ({len(nouveaux_dns)} autre(s) entrée(s) système)",
            }
        else:
            ctrl_dns = {"etat": "echec", "detail": f"entrée(s) DNS suspecte(s) : {', '.join(dns_suspects)}"}

    # 5. Journal du serveur actif (octets écrits pendant le test uniquement)
    ctrl_log, meta_log = verifier_canari_journal(jeton_canari, journal, taille_journal_avant)

    # 6. Durcissement EFFECTIF du serveur actif (lu dans son journal, pas dans ce shell)
    config_serveur = lire_config_serveur(journal)
    ctrl_env = verifier_durcissement_serveur(config_serveur)

    # 7. Pare-feu sortant Windows Defender
    ctrl_fw = verifier_pare_feu_windows()

    # 8. Capture paquets PktMon (séparée : non_execute si absente)
    if args.capture_pcap:
        p_cap = Path(args.capture_pcap)
        if p_cap.exists() and p_cap.stat().st_size > 0:
            ctrl_pcap = {"etat": "reussi", "detail": f"capture présente ({p_cap.name}, {p_cap.stat().st_size} octets ; analyse Wireshark requise)"}
        else:
            ctrl_pcap = {"etat": "echec", "detail": f"fichier capture introuvable ou vide : {args.capture_pcap}"}
    else:
        ctrl_pcap = {
            "etat": "non_execute",
            "detail": "aucune capture PktMon fournie (--capture-pcap) ; requiert PowerShell Administrateur",
        }

    controles = {
        "binding_loopback": ctrl_binding,
        "echo_canari_exact": ctrl_canari,
        "zero_connexion_distante": ctrl_tcp,
        "zero_requete_dns_canari": ctrl_dns,
        "zero_canari_dans_server_log": ctrl_log,
        "variables_durcissement": {"etat": ctrl_env["etat"], "detail": ctrl_env["detail"]},
        "pare_feu_sortant_bloque": ctrl_fw,
        "capture_paquets_pktmon": ctrl_pcap,
    }

    controles_stricts = (
        "binding_loopback",
        "echo_canari_exact",
        "zero_connexion_distante",
        "zero_requete_dns_canari",
        "zero_canari_dans_server_log",
        "variables_durcissement",
        "pare_feu_sortant_bloque",
    )
    controles_coeur = (
        "binding_loopback",
        "echo_canari_exact",
        "zero_connexion_distante",
        "zero_requete_dns_canari",
        "zero_canari_dans_server_log",
    )

    if all(controles[k]["etat"] == "reussi" for k in controles_stricts):
        statut = "PASS"
    elif all(controles[k]["etat"] == "reussi" for k in controles_coeur):
        statut = "PARTIAL"
    else:
        statut = "FAIL"

    rapport = {
        "horodatage": horodatage,
        "statut": statut,
        "marqueur_canari": jeton_canari,
        "versions": versions,
        "mesures_inference": mesures_inf,
        "ollama_ps": ollama_ps,
        "echantillonnage_tcp": {
            "nb_echantillons": nb_echantillons,
            "fenetre_s": fenetre_s,
            "intervalle_s": INTERVALLE_POLLING_S,
        },
        "fichiers_inspectes": {"journal_serveur": meta_log},
        "config_serveur": config_serveur,
        "limites_capture": [
            f"Le polling Get-NetTCPConnection (pause {INTERVALLE_POLLING_S}s + lancement PowerShell, intervalle effectif "
            f"{round(fenetre_s / nb_echantillons, 2) if nb_echantillons else '?'}s sur ce test) peut manquer une connexion TCP "
            "très brève ; il ne voit pas l'UDP (DNS/UDP, QUIC).",
            "Get-DnsClientCache ne détecte ni les connexions vers une IP en dur ni DNS-over-HTTPS (DoH), "
            "ni les résolutions déjà en cache avant le test.",
            "Seuls les processus ollama* et llama-server sont surveillés.",
            "variables_durcissement lit la configuration écrite par le serveur dans son journal au démarrage ; "
            "OLLAMA_DEBUG_LOG_REQUESTS rendrait le contrôle du journal caduc.",
            "Seuls le pare-feu OS, une capture PktMon/Wireshark ou Docker network_mode=none couvrent le niveau paquet/noyau.",
        ],
        "controles": controles,
    }

    if args.json:
        print(json.dumps(rapport, ensure_ascii=False, indent=2))
    else:
        print(f"=== Audit d'isolation locale Ollama ({args.modele}) · {horodatage} ===")
        print(f"  Ollama={versions['ollama_api_version']} | Digest={versions['modele_digest']}")
        print(f"  Marqueur={jeton_canari}")
        for nom, ctrl in controles.items():
            print(f"  [{ctrl['etat'].upper()}] {nom} : {ctrl['detail']}")
        if ollama_ps.get("ligne_modele"):
            print(f"  ollama ps : {ollama_ps['ligne_modele']}")
        print(f"Verdict global : {statut}")

    if args.strict and statut != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
