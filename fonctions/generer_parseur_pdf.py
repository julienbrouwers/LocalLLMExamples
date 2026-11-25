"""Demande à un LLM local Ollama de générer la fonction Python `parse_articles(lines)`
pour extraire les articles des factures PDF, puis exécute et vérifie le code généré
sur les 4 factures PDF réelles (`FA000101.pdf` à `FA000104.pdf`).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError

import pymupdf
from openpyxl import Workbook

from commun import RACINE, appeler_ollama, decharger_modele, extraire_mesures


DOSSIER_FACTURES_DEFAUT: Path = RACINE / "donnees" / "entrees" / "factures"
DOSSIER_PROMPTS: Path = Path(__file__).resolve().parent.parent / "prompts"

VERITE_TERRAIN: dict[str, list[tuple[str, int]]] = {
    "FA000101.pdf": [("Bloodshot Intégrale", 1)],
    "FA000102.pdf": [("Ultimate Spider-Man : La mort de Spider-Man", 1)],
    "FA000103.pdf": [
        ("Les Tortues Ninja - TMNT Classics Tome 3", 1),
        ("TMNT Tome 17 : Lignes de Front", 1),
    ],
    "FA000104.pdf": [
        ("Alien Tome 2 : Renouveau", 1),
        ("Alien Tome 3 : Icarus", 1),
        ("Aliens Volume 2 Omnibus", 1),
        ("Marque-page Excalibur - The Witcher", 1),
    ],
}


def extraire_lignes_pdf(chemin_pdf: Path) -> list[str]:
    """Extrait les lignes texte non vides d'un PDF via `pymupdf` (un LLM ne lit pas un PDF binaire)."""
    lignes: list[str] = []
    with pymupdf.open(chemin_pdf) as pdf:
        for page in pdf:
            lignes.extend(ligne.strip() for ligne in page.get_text().splitlines() if ligne.strip())
    return lignes


def extraire_date_pdf(chemin_pdf: Path) -> date:
    """Extrait la date d'émission (`JJ/MM/AAAA`) de la première page d'une facture PDF."""
    with pymupdf.open(chemin_pdf) as pdf:
        entete = "\n".join(pdf[0].get_text().splitlines()[:8])
    correspondance = re.search(r"\b\d{2}/\d{2}/\d{4}\b", entete)
    if not correspondance:
        raise ValueError(f"Date introuvable dans {chemin_pdf.name}.")
    return datetime.strptime(correspondance.group(), "%d/%m/%Y").date()


def options_par_modele(modele: str, contexte: int, temperature: float = 0.0, seed: int = 42) -> dict[str, Any]:
    """Options Ollama alignées sur les docs officielles de chaque famille de LLM."""
    options: dict[str, Any] = {
        "temperature": temperature,
        "seed": seed,
        "num_ctx": contexte,
        "num_predict": 1000,
    }
    if modele == "llama3.2:3b":
        options["repeat_penalty"] = 1.15
    elif modele.startswith("qwen2.5"):
        options["repeat_penalty"] = 1.05
        options["top_p"] = 0.8
        options["top_k"] = 20
    return options


def charger_prompt(modele: str, langue: str, fichier_prompt: Path | None) -> tuple[str, Path]:
    """Charge le prompt externe (`prompts/pdf-codegen-*-{fr|eng}.txt` ou `--prompt`)."""
    if fichier_prompt is not None:
        return fichier_prompt.read_text(encoding="utf-8").strip(), fichier_prompt

    suffixe = "eng" if langue == "eng" else "fr"
    if modele == "llama3.2:3b":
        cible = DOSSIER_PROMPTS / f"pdf-codegen-llama-{suffixe}.txt"
    elif modele.startswith("qwen2.5"):
        cible = DOSSIER_PROMPTS / f"pdf-codegen-qwen-{suffixe}.txt"
    else:
        cible = DOSSIER_PROMPTS / f"pdf-codegen-mistral-{suffixe}.txt"

    if not cible.is_file():
        raise SystemExit(f"Fichier prompt introuvable : {cible}")
    return cible.read_text(encoding="utf-8").strip(), cible


def nettoyer_code_python(code_brut: str) -> str:
    """Retire d'éventuelles balises markdown ```python ... ``` autour du code généré."""
    texte = code_brut.strip()
    correspondance = re.search(r"```(?:python)?\s*(.*?)```", texte, re.S | re.I)
    return correspondance.group(1).strip() if correspondance else texte


def verifier_code_genere(
    code: str,
    mesures: dict[str, Any],
    dossier_factures: Path,
) -> tuple[dict[str, Any], dict[str, list[tuple[str, int]]]]:
    """Compile et exécute `parse_articles` généré par le LLM sur toutes les factures PDF réelles."""
    controles: dict[str, dict[str, Any]] = {}
    extractions: dict[str, list[tuple[str, int]]] = {}

    controles["json_et_stop"] = {
        "ok": bool(code) and mesures.get("done_reason") == "stop",
        "detail": f"done_reason={mesures.get('done_reason')}",
    }

    espace: dict[str, Any] = {}
    try:
        exec(compile(code, "<llm_parse_articles>", "exec"), espace)
        fn = espace.get("parse_articles")
        compil_ok = callable(fn)
        detail_compil = "fonction `parse_articles` chargée" if compil_ok else "`parse_articles` absente"
    except Exception as erreur:
        fn = None
        compil_ok = False
        detail_compil = f"{type(erreur).__name__}: {erreur}"

    controles["compilation"] = {"ok": compil_ok, "detail": detail_compil}

    if not compil_ok or fn is None:
        controles["factures_reelles"] = {"ok": False, "detail": "non exécuté (compilation KO)"}
        controles["garde_erreur"] = {"ok": False, "detail": "non exécuté (compilation KO)"}
        return {"statut": "FAIL", "controles": controles}, extractions

    total_attendus = 0
    total_exacts = 0
    ecarts: list[str] = []
    for chemin_pdf in sorted(dossier_factures.glob("*.pdf")):
        lignes = extraire_lignes_pdf(chemin_pdf)
        attendu = VERITE_TERRAIN.get(chemin_pdf.name)
        try:
            obtenu = fn(lignes)
            extractions[chemin_pdf.name] = obtenu
            if attendu is not None:
                total_attendus += len(attendu)
                if obtenu == attendu:
                    total_exacts += len(attendu)
                else:
                    ecarts.append(f"{chemin_pdf.name} (obtenu={obtenu})")
        except Exception as erreur:
            ecarts.append(f"{chemin_pdf.name} ({type(erreur).__name__}: {erreur})")

    factures_ok = total_attendus > 0 and total_exacts == total_attendus and not ecarts
    controles["factures_reelles"] = {
        "ok": factures_ok,
        "detail": f"{total_exacts}/{total_attendus} articles exacts sur 4 PDF"
        + (f" — écarts: {', '.join(ecarts)}" if ecarts else ""),
    }

    # Vérifier la levée de ValueError sur entrée invalide
    try:
        fn(["ligne sans repère"])
        garde_ok = False
        detail_garde = "aucune ValueError levée sur entrée invalide"
    except ValueError:
        garde_ok = True
        detail_garde = "ValueError bien levée sur entrée invalide"
    except Exception as erreur:
        garde_ok = False
        detail_garde = f"exception inattendue {type(erreur).__name__}"

    controles["garde_erreur"] = {"ok": garde_ok, "detail": detail_garde}

    if all(c["ok"] for c in controles.values()):
        statut = "PASS"
    elif factures_ok:
        statut = "PARTIAL"
    else:
        statut = "FAIL"

    return {"statut": statut, "controles": controles}, extractions


def exporter_excel(dossier_factures: Path, extractions: dict[str, list[tuple[str, int]]], sortie_xlsx: Path) -> int:
    """Écrit l'inventaire Excel consolidé au format exact attendu par `commun.py`."""
    classeur = Workbook()
    feuille = classeur.active
    feuille.title = "Inventaire"
    feuille.append(["Facture", "Date", "Page", "Article", "Libellé", "Quantité"])
    total = 0
    for chemin_pdf in sorted(dossier_factures.glob("*.pdf")):
        jour = extraire_date_pdf(chemin_pdf)
        for pos, (libelle, qte) in enumerate(extractions.get(chemin_pdf.name, []), 1):
            feuille.append([chemin_pdf.name, jour, 1, pos, libelle, qte])
            feuille.cell(feuille.max_row, 2).number_format = "dd/mm/yyyy"
            total += 1
    feuille.freeze_panes = "A2"
    classeur.save(sortie_xlsx)
    return total


def construire_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modele", default="qwen2.5:3b", help="Modèle Ollama (par défaut : qwen2.5:3b)")
    parser.add_argument("--dossier-factures", type=Path, default=DOSSIER_FACTURES_DEFAUT, help="Dossier des factures PDF")
    parser.add_argument("--facture-exemple", default="FA000104.pdf", help="PDF témoin envoyé en texte brut au LLM")
    parser.add_argument("--prompt", type=Path, metavar="FICHIER", help="Fichier prompt externe (sinon prompts/pdf-codegen-*-{fr|eng}.txt)")
    parser.add_argument("--langue", choices=("fr", "eng"), default="fr", help="Langue du prompt externe (`fr` ou `eng`)")
    parser.add_argument("--contexte", type=int, default=4096, metavar="TOKENS", help="Contexte Ollama (par défaut : 4096)")
    parser.add_argument("--temperature", type=float, default=0.0, help="Température d'échantillonnage (par défaut : 0.0)")
    parser.add_argument("--seed", type=int, default=42, help="Graine aléatoire Ollama (par défaut : 42)")
    parser.add_argument("--sortie-excel", type=Path, metavar="FICHIER", help="Exporte l'inventaire Excel si le code généré réussit")
    parser.add_argument("--mesures", action="store_true", help="Affiche les compteurs Ollama sur stderr")
    return parser


def main() -> None:
    parser = construire_parser()
    args = parser.parse_args()

    if args.prompt is not None and not args.prompt.is_file():
        raise SystemExit(f"Fichier prompt introuvable : {args.prompt}")

    est_eng = args.langue == "eng" or (args.prompt is not None and args.prompt.stem.endswith("-eng"))
    systeme, source_prompt = charger_prompt(args.modele, "eng" if est_eng else "fr", args.prompt)

    chemin_exemple = args.dossier_factures / args.facture_exemple
    if not chemin_exemple.is_file():
        raise SystemExit(f"Facture témoin introuvable : {chemin_exemple}")

    lignes_exemple = extraire_lignes_pdf(chemin_exemple)
    # Réduire le bruit d'entrée : envoyer uniquement la tranche autour du tableau d'articles en vraie liste Python
    debut_extrait = max(0, lignes_exemple.index("Produit") - 1) if "Produit" in lignes_exemple else 0
    fin_extrait = next((i + 2 for i, l in enumerate(lignes_exemple) if l.startswith("Détail des")), len(lignes_exemple))
    extrait_liste = json.dumps(lignes_exemple[debut_extrait:fin_extrait], ensure_ascii=False, indent=2)
    contenu_user = (
        (
            f"Sample `lines: list[str]` excerpt extracted by `pymupdf` from `{chemin_exemple.name}`:\n"
            f"```python\nlines = {extrait_liste}\n```\n"
            "Write `parse_articles(lines: list[str]) -> list[tuple[str, int]]`."
        )
        if est_eng
        else (
            f"Extrait `lines: list[str]` extrait par `pymupdf` depuis `{chemin_exemple.name}` :\n"
            f"```python\nlines = {extrait_liste}\n```\n"
            "Écris `parse_articles(lines: list[str]) -> list[tuple[str, int]]`."
        )
    )

    schema = {
        "type": "object",
        "required": ["code", "explication"],
        "additionalProperties": False,
        "properties": {
            "code": {"type": "string", "minLength": 20},
            "explication": {"type": "string", "minLength": 5},
        },
    }
    donnees = {
        "model": args.modele,
        "stream": False,
        "keep_alive": 0,
        "options": options_par_modele(args.modele, args.contexte, args.temperature, args.seed),
        "format": schema,
        "messages": [
            {"role": "system", "content": systeme},
            {"role": "user", "content": contenu_user},
        ],
    }

    try:
        reponse = appeler_ollama("chat", donnees, timeout=300)
    except HTTPError as erreur:
        decharger_modele(args.modele)
        raise SystemExit(f"Ollama a renvoyé HTTP {erreur.code}.") from None
    except (URLError, TimeoutError) as erreur:
        decharger_modele(args.modele)
        raise SystemExit(f"Ollama injoignable : {erreur}") from None

    duree_s = round(reponse.get("total_duration", 0) / 1e9, 2)
    mesures = extraire_mesures(reponse)

    try:
        payload = json.loads(reponse["message"]["content"])
        code = nettoyer_code_python(payload.get("code", ""))
        explication = payload.get("explication", "")
    except ValueError as erreur:
        code = ""
        explication = f"JSON invalide : {erreur}"

    verification, extractions = verifier_code_genere(code, mesures, args.dossier_factures)

    print(f"\n=== Génération `parse_articles` · {args.modele} ({duree_s}s · {mesures.get('prompt_eval_count')} tok in -> {mesures.get('eval_count')} tok out · {mesures.get('done_reason')}) ===")
    print(f"Prompt : {source_prompt}")
    print(f"Explication : {explication}")
    print("Code Python généré :")
    print(code)
    print("\nExtractions sur les 4 factures PDF réelles :")
    for nom_pdf, articles in extractions.items():
        print(f"  - {nom_pdf} : {articles}")
    print(f"\nVérification locale : {verification['statut']}")
    for nom_ctrl, res_ctrl in verification["controles"].items():
        marque = "OK" if res_ctrl["ok"] else "KO"
        print(f"  [{marque}] {nom_ctrl} : {res_ctrl['detail']}")

    if args.sortie_excel is not None and verification["statut"] == "PASS":
        nb = exporter_excel(args.dossier_factures, extractions, args.sortie_excel)
        print(f"\nInventaire Excel généré : {args.sortie_excel} ({nb} articles)")

    if args.mesures:
        print(json.dumps(mesures), file=sys.stderr)


if __name__ == "__main__":
    main()
