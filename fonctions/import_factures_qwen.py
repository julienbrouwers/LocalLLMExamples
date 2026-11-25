"""Crée un inventaire Excel consolidé à partir des factures PDF Excalibur Comics."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import re
from typing import Sequence

import pymupdf
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet


ENTETE_INVENTAIRE: list[str] = ["Facture", "Date", "Page", "Article", "Libellé", "Quantité"]
LARGEURS_COLONNES: dict[str, int] = {
    "A": 19,
    "B": 14,
    "C": 9,
    "D": 10,
    "E": 55,
    "F": 11,
}


def parse_articles(lines: Sequence[str]) -> list[tuple[str, int]]:
    """Extrait les couples `(libellé, quantité)` du bloc d'articles d'une page de facture."""
    product_line = lines.index("Produit")
    total_line = lines.index("Total", product_line + 1)

    article_block_start = total_line + 1
    article_block_end = next(
        (
            index
            for index, line in enumerate(lines[article_block_start:], article_block_start)
            if line.startswith("Détail des")
        ),
        None,
    )
    if article_block_end is None:
        raise ValueError("Line with 'Détail des' missing")

    article_block = lines[article_block_start:article_block_end]
    if not article_block or len(article_block) % 5 != 0:
        raise ValueError("Invalid article block length or structure.")

    parsed_articles: list[tuple[str, int]] = []
    for index in range(0, len(article_block), 5):
        title = article_block[index].strip()
        quantity = int(article_block[index + 3])
        parsed_articles.append((title, quantity))

    return parsed_articles


def extraire_date_facture(pdf: pymupdf.Document, nom_fichier: str) -> date:
    """Repère la date d'émission (`JJ/MM/AAAA`) dans l'en-tête de la première page."""
    premieres_lignes = "\n".join(pdf[0].get_text().splitlines()[:8])
    correspondance = re.search(r"\b\d{2}/\d{2}/\d{4}\b", premieres_lignes)
    if not correspondance:
        raise ValueError(f"Date introuvable dans {nom_fichier}.")
    return datetime.strptime(correspondance.group(), "%d/%m/%Y").date()


def mettre_en_forme_feuille(feuille: Worksheet) -> None:
    """Fige l'en-tête, active le filtre automatique et ajuste la largeur des colonnes."""
    feuille.freeze_panes = "A2"
    feuille.auto_filter.ref = feuille.dimensions
    for colonne, largeur in LARGEURS_COLONNES.items():
        feuille.column_dimensions[colonne].width = largeur


def construire_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments en ligne de commande."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path, help="Dossier contenant les factures PDF")
    parser.add_argument("sortie", type=Path, help="Chemin du classeur Excel (.xlsx) à générer")
    return parser


def main() -> None:
    """Point d'entrée du script d'import PDF vers Excel."""
    parser = construire_parser()
    args = parser.parse_args()

    chemins_pdf = sorted(args.dossier.glob("*.pdf"))
    if not chemins_pdf:
        parser.error("Aucune facture PDF dans le dossier.")

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = "Inventaire"
    feuille.append(ENTETE_INVENTAIRE)

    try:
        for chemin in chemins_pdf:
            lignes_avant = feuille.max_row
            with pymupdf.open(chemin) as pdf:
                jour = extraire_date_facture(pdf, chemin.name)
                for numero_page, page in enumerate(pdf, 1):
                    lignes = [ligne.strip() for ligne in page.get_text().splitlines() if ligne.strip()]
                    if "Produit" not in lignes:
                        continue
                    for position, (libelle, quantite) in enumerate(parse_articles(lignes), 1):
                        feuille.append([chemin.name, jour, numero_page, position, libelle, quantite])
                        feuille.cell(feuille.max_row, 2).number_format = "dd/mm/yyyy"
                        feuille.cell(feuille.max_row, 5).data_type = "s"

            if feuille.max_row == lignes_avant:
                raise ValueError(f"Aucun article trouvé dans {chemin.name}.")

        mettre_en_forme_feuille(feuille)
        classeur.save(args.sortie)
    except (OSError, RuntimeError, ValueError) as erreur:
        parser.exit(2, f"Import interrompu : {erreur}\n")

    print(f"{len(chemins_pdf)} factures, {feuille.max_row - 1} articles dans {args.sortie}.")


if __name__ == "__main__":
    main()
