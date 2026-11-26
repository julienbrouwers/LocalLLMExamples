"""Audite et vérifie l'isolation réseau d'Ollama pendant une inférence locale réelle.

Produit un résultat explicite par contrôle parmi :
- `reussi`        : contrôle exécuté et conforme
- `echec`         : contrôle exécuté et non conforme
- `non_execute`   : contrôle non exécuté (plateforme, prérequis ou capture absente)
- `non_concluant` : contrôle exécuté mais preuve insuffisante (ex. fichier log absent)

Enregistre aussi les versions et digests (Ollama `/api/version`, modèle `/api/tags`),
les temps (`total_duration`, `load_duration`), la colonne `PROCESSOR` de `ollama ps`,
la fenêtre et le nombre d'échantillons TCP, les fichiers inspectés et les limites de mesure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from commun import OLLAMA_URL, appeler_ollama, decharger_modele, extraire_mesures


ADRESSE_LOOPBACK = {"127.0.0.1", "::1", "0.0.0.0", "::", ""}
REGLES_PARE_FEU_SCRIPT = (
    "Ollama-Block-Outbound-LocalOnly",
    "Ollama-Block-Outbound-App-LocalOnly",
)
INTERVALLE_POLLING_S = 0.35


def _executer_powershell(commande: str) -> str:
    """Exécute une commande PowerShell en lecture seule et renvoie stdout."""
    res = subprocess.run(
        ["powershell", "-NoProfile", "-Command", commande],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return res.stdout.strip()


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


def inspecter_sockets_ollama() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Retourne `(ecoutes, connexions_distantes)` pour les processus `ollama*`."""
    if platform.system() != "Windows":
        return [], []

    ps = (
        "$p = (Get-Process -Name 'ollama*' -ErrorAction SilentlyContinue).Id; "
        "if (-not $p) { '[]'; exit 0 } "
        "$c = @(Get-NetTCPConnection -OwningProcess $p -ErrorAction SilentlyContinue | "
        "Select-Object OwningProcess, State, LocalAddress, LocalPort, RemoteAddress, RemotePort); "
        "$c | ConvertTo-Json -Compress"
    )
    sortie = _executer_powershell(ps)
    if not sortie:
        return [], []
    try:
        donnees = json.loads(sortie)
    except ValueError:
        return [], []
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
    return ecoutes, distantes


def verifier_pare_feu_windows() -> dict[str, Any]:
    """Vérifie si les règles pare-feu sortantes bloquantes du script sont actives sur Windows."""
    if platform.system() != "Windows":
        return {"etat": "non_execute", "detail": "vérification pare-feu automatique dédiée à Windows"}

    noms_ps = ", ".join(f"'{n}'" for n in REGLES_PARE_FEU_SCRIPT)
    ps = (
        f"$r = @(Get-NetFirewallRule -Name {noms_ps} -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Direction -eq 'Outbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True' } | "
        "Select-Object -ExpandProperty Name); "
        "$r | ConvertTo-Json -Compress"
    )
    sortie = _executer_powershell(ps)
    if not sortie:
        regles: list[str] = []
    else:
        try:
            brut = json.loads(sortie)
            regles = [brut] if isinstance(brut, str) else list(brut)
        except ValueError:
            regles = []

    if regles:
        return {"etat": "reussi", "detail": f"règle(s) bloquante(s) active(s) : {', '.join(regles)}"}
    return {"etat": "echec", "detail": "aucune règle sortante Block créée par ce script n'est active"}


def verifier_env_durcissement() -> dict[str, Any]:
    """Vérifie les variables d'environnement de session (`OLLAMA_HOST`, `OLLAMA_NO_CLOUD`, pas de CORS `*`)."""
    attendues = {
        "OLLAMA_HOST": os.environ.get("OLLAMA_HOST", ""),
        "OLLAMA_NO_CLOUD": os.environ.get("OLLAMA_NO_CLOUD", ""),
    }
    origins = os.environ.get("OLLAMA_ORIGINS", "")
    manquantes = [k for k, v in attendues.items() if not v]

    host_val = attendues["OLLAMA_HOST"].removeprefix("http://").removeprefix("https://").split("/")[0]
    host_only = host_val.rsplit(":", 1)[0].lower() if ":" in host_val and not host_val.endswith("]") else host_val.lower()
    host_ok = (not host_only) or host_only in ("127.0.0.1", "localhost", "[::1]", "::1")
    origins_ok = "*" not in {o.strip() for o in origins.split(",") if o.strip()}

    if not origins_ok:
        return {
            "etat": "echec",
            "detail": "DANGER : OLLAMA_ORIGINS contient '*' (toute page web peut interroger Ollama local)",
            "valeurs": attendues,
        }
    if manquantes:
        return {
            "etat": "echec",
            "detail": f"non définies dans cette session : {', '.join(manquantes)}",
            "valeurs": attendues,
        }
    if not host_ok:
        return {
            "etat": "echec",
            "detail": f"OLLAMA_HOST exposé hors loopback : {attendues['OLLAMA_HOST']}",
            "valeurs": attendues,
        }
    return {
        "etat": "reussi",
        "detail": ", ".join(f"{k}={v}" for k, v in attendues.items())
        + " (variables lues côté session ; redémarrez ollama serve pour les appliquer au serveur)",
        "valeurs": attendues,
    }


def lire_cache_dns_windows() -> set[str]:
    """Retourne les noms de domaines présents dans le cache DNS Windows."""
    if platform.system() != "Windows":
        return set()
    ps = (
        "@(Get-DnsClientCache -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty Entry -Unique) | ConvertTo-Json -Compress"
    )
    sortie = _executer_powershell(ps)
    if not sortie:
        return set()
    try:
        brut = json.loads(sortie)
        if isinstance(brut, str):
            return {brut.lower()}
        return {str(x).lower() for x in brut}
    except ValueError:
        return set()


def verifier_canari_server_log(jeton: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Vérifie si le jeton canari apparaît en clair dans `%LOCALAPPDATA%\\Ollama\\server.log`."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    if not local_app:
        meta = {"chemin": None, "present": False, "taille_octets": 0}
        return {"etat": "non_execute", "detail": "LOCALAPPDATA absent (hors Windows)"}, meta

    log_path = Path(local_app) / "Ollama" / "server.log"
    if not log_path.exists():
        meta = {"chemin": str(log_path), "present": False, "taille_octets": 0}
        return {"etat": "non_concluant", "detail": f"{log_path} absent à cet emplacement"}, meta

    try:
        taille = log_path.stat().st_size
        meta = {"chemin": str(log_path), "present": True, "taille_octets": taille}
        with log_path.open("rb") as f:
            if taille > 262_144:
                f.seek(taille - 262_144)
            extrait = f.read().decode("utf-8", errors="ignore")
        if jeton in extrait:
            return {"etat": "echec", "detail": f"jeton '{jeton}' détecté en clair dans {log_path.name}"}, meta
        return {"etat": "reussi", "detail": f"0 occurrence du marqueur dans les 256 derniers Ko de {log_path.name}"}, meta
    except OSError as exc:
        meta = {"chemin": str(log_path), "present": True, "taille_octets": 0}
        return {"etat": "non_concluant", "detail": f"lecture impossible de server.log : {exc}"}, meta


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

    ecoutes_avant, distantes_avant = inspecter_sockets_ollama()
    dns_avant = lire_cache_dns_windows()

    # 1. Binding strict loopback
    if not est_windows:
        ctrl_binding = {"etat": "non_execute", "detail": "inspection Get-NetTCPConnection dédiée à Windows"}
    else:
        exposees = [
            f"{e.get('LocalAddress')}:{e.get('LocalPort')}"
            for e in ecoutes_avant
            if str(e.get("LocalAddress")) not in ("127.0.0.1", "::1")
        ]
        if not ecoutes_avant:
            ctrl_binding = {"etat": "non_concluant", "detail": "aucun socket Listen détecté pour ollama*"}
        elif exposees:
            ctrl_binding = {"etat": "echec", "detail": f"socket(s) exposé(s) hors loopback : {', '.join(exposees)}"}
        else:
            detail = ", ".join(f"{e.get('LocalAddress')}:{e.get('LocalPort')}" for e in ecoutes_avant)
            ctrl_binding = {"etat": "reussi", "detail": detail}

    # 2. Inférence locale sur le marqueur canari + surveillance TCP continue
    distantes_pendant: list[dict[str, Any]] = []
    nb_echantillons = 0
    stop_event = threading.Event()

    def _surveiller_sockets() -> None:
        nonlocal nb_echantillons
        while not stop_event.is_set():
            _, dist = inspecter_sockets_ollama()
            nb_echantillons += 1
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
        if echo_exact:
            ctrl_canari = {
                "etat": "reussi",
                "detail": f"écho exact en {total_s}s (chargement={load_s}s, {mesures.get('prompt_eval_count')} -> {mesures.get('eval_count')} tok)",
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
    _, distantes_apres = inspecter_sockets_ollama()
    dns_apres = lire_cache_dns_windows()

    # 3. Connexions TCP distantes observées
    if not est_windows:
        ctrl_tcp = {"etat": "non_execute", "detail": "surveillance TCP dédiée à Windows"}
    else:
        toutes_distantes = distantes_avant + distantes_pendant + distantes_apres
        if not toutes_distantes:
            ctrl_tcp = {
                "etat": "reussi",
                "detail": f"0 connexion TCP distante sur ollama* ({nb_echantillons} échantillon(s) sur {fenetre_s}s)",
            }
        else:
            ctrl_tcp = {
                "etat": "echec",
                "detail": f"{len(toutes_distantes)} connexion(s) distante(s) détectée(s) : {toutes_distantes}",
            }

    # 4. Cache DNS Windows
    if not est_windows:
        ctrl_dns = {"etat": "non_execute", "detail": "cache DNS dédié à Windows"}
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

    # 5. Journal server.log
    ctrl_log, meta_log = verifier_canari_server_log(jeton_canari)

    # 6. Variables d'environnement de session
    ctrl_env = verifier_env_durcissement()

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
        "fichiers_inspectes": {"server_log": meta_log},
        "limites_capture": [
            f"Le polling Get-NetTCPConnection ({INTERVALLE_POLLING_S}s) peut manquer une connexion TCP très brève ou un flux UDP.",
            "Get-DnsClientCache ne détecte ni les connexions vers une IP en dur ni DNS-over-HTTPS (DoH).",
            "variables_durcissement lit l'environnement de la session cliente, pas celui d'un serveur ollama.exe déjà démarré.",
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
