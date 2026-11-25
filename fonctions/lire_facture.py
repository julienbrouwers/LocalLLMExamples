"""Extrait le texte d'une facture PDF et interroge Ollama pour obtenir ses champs en JSON."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.error import HTTPError, URLError

import pymupdf

from commun import appeler_ollama


def extraire_texte_pdf(chemin_pdf: Path) -> str:
    """Extrait et concatène le texte de toutes les pages d'un fichier PDF."""
    with pymupdf.open(chemin_pdf) as document:
        texte = "\n\n".join(page.get_text(sort=True) for page in document)

    if not texte.strip():
        raise ValueError("Aucun texte trouvé. Le PDF est peut-être un scan : un OCR est nécessaire.")
    return texte


def construire_prompt_facture(texte: str) -> str:
    """Construit la consigne d'extraction structurée des champs de la facture."""
    return (
        "Tu es chargé d'extraire les informations d'une facture.\n"
        "Utilise uniquement les informations présentes dans le document.\n"
        "Si une information est absente ou illisible, indique null.\n"
        "Réponds uniquement en JSON avec ces champs :\n"
        "- fournisseur\n"
        "- numero_facture\n"
        "- date_facture\n"
        "- date_echeance\n"
        "- montant_ht\n"
        "- montant_tva\n"
        "- montant_ttc\n"
        "- devise\n\n"
        "Texte de la facture :\n"
        f"<facture>\n{texte}\n</facture>"
    )


def construire_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments en ligne de commande."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Chemin de la facture PDF à analyser")
    parser.add_argument(
        "--modele",
        default="mistral:7b",
        help="Modèle Ollama utilisé pour l'extraction (par défaut : mistral:7b)",
    )
    return parser


def main() -> None:
    """Point d'entrée du script d'extraction JSON de facture."""
    parser = construire_parser()
    args = parser.parse_args()

    try:
        texte = extraire_texte_pdf(args.pdf)
    except (OSError, RuntimeError, ValueError) as erreur:
        raise SystemExit(f"Lecture du PDF impossible ({args.pdf}) : {erreur}") from None

    donnees = {
        "model": args.modele,
        "prompt": construire_prompt_facture(texte),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }

    try:
        resultat = appeler_ollama("generate", donnees, timeout=300)
    except HTTPError as erreur:
        raise SystemExit(f"Ollama a renvoyé une erreur HTTP {erreur.code}.") from None
    except (URLError, TimeoutError) as erreur:
        raise SystemExit(f"Ollama est injoignable : {erreur}") from None

    print(resultat["response"])


if __name__ == "__main__":
    main()
