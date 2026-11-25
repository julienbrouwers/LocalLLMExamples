"""Classe les livres de l'inventaire par genre/catégorie par paquets et met à jour le CSV progressivement."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from commun import (
    CACHE_API_DEFAUT,
    CATEGORIES_INVENTAIRE,
    CLASSEUR_DEFAUT,
    COLONNES_INVENTAIRE_CATEGORISE,
    appeler_ollama,
    generer_lignes_fictives,
    lire_lignes_inventaire,
)


# --- Historique des prompts ---
# Prompt V1 (historisé) : classement par univers sans règle métier sur les objets dérivés.
# Limite observée : « Marque-page Excalibur - The Witcher » est classé en « Fantasy » (attiré par Excalibur / The Witcher).
# Règle accessoire V1 (historisée) : citait « un marque-page, une affiche ou un carnet », ce qui classait par erreur
# le livre fictif « Carnets de la Citadelle Tome 1 » en « Accessoire / Papeterie ».
REGLE_ACCESSOIRE_V1_HISTORIQUE: str = (
    " Attention : si le libellé indique un objet dérivé ou de papeterie (par exemple un marque-page, "
    "une affiche ou un carnet), classe-le obligatoirement dans « Accessoire / Papeterie », même s'il "
    "porte le nom d'une œuvre de Fantasy ou de Science-fiction."
)

# Prompt V2 amélioré (actif par défaut) : précise « sans numéro de Tome ou de Volume » pour isoler
# les vrais accessoires (marque-pages, affiches) sans piéger les livres intitulés « Carnets de... Tome 1 ».
REGLE_ACCESSOIRE_V2_AMELIOREE: str = (
    " Attention : si le libellé désigne un objet dérivé de papeterie (comme un marque-page ou une affiche, "
    "sans numéro de Tome ou de Volume), classe-le obligatoirement dans « Accessoire / Papeterie », même "
    "s'il porte le nom d'une œuvre de Fantasy ou de Science-fiction."
)

# Constat (Qwen 2.5 3B) : même avec une règle explicite dans le prompt, des livres « Carnets de la Citadelle
# Tome 4 » partent encore en papeterie. La contrainte est donc posée dans le schéma JSON : un libellé
# numéroté (Tome / Volume + chiffre) ne peut pas recevoir la catégorie « Accessoire / Papeterie ».
CATEGORIE_ACCESSOIRE: str = "Accessoire / Papeterie"
MOTIF_LIVRE_NUMEROTE = re.compile(r"\b(tome|volume|vol\.?)\s*\d", re.IGNORECASE)

REQUETES_CATALOGUE_API: tuple[tuple[str, str], ...] = (
    ("fantasy", "Fantasy"),
    ("science fiction", "Science-fiction / Horreur"),
    ("horreur stephen king lovecraft", "Science-fiction / Horreur"),
    ("batman spider-man marvel comics", "Super-héros / Action"),
    ("aventure jules verne dumas", "Aventure / Exploration"),
)

TITRES_PDF_ORIGINAUX: frozenset[str] = frozenset(
    {
        "bloodshot intégrale",
        "ultimate spider-man : la mort de spider-man",
        "les tortues ninja - tmnt classics tome 3",
        "tmnt tome 17 : lignes de front",
        "alien tome 2 : renouveau",
        "alien tome 3 : icarus",
        "aliens volume 2 omnibus",
        "marque-page excalibur - the witcher",
    }
)


def recuperer_livres_api(
    nombre: int,
    fichier_cache: Path = CACHE_API_DEFAUT,
    rafraichir: bool = False,
) -> list[list[Any]]:
    """Récupère de vrais titres de livres publiés en français via API publique (avec cache local)."""
    catalogue: list[dict[str, str]] = []
    if not rafraichir and fichier_cache.exists():
        try:
            catalogue = json.loads(fichier_cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalogue = []

    if len(catalogue) < nombre:
        par_sujet = max((nombre // len(REQUETES_CATALOGUE_API)) + 8, 25)
        listes_par_sujet: list[list[dict[str, str]]] = []
        vus = set(TITRES_PDF_ORIGINAUX)

        for terme_q, genre_indicatif in REQUETES_CATALOGUE_API:
            params = urlencode(
                {
                    "term": terme_q,
                    "country": "fr",
                    "media": "ebook",
                    "limit": min(par_sujet * 2, 50),
                }
            )
            requete = Request(
                f"https://itunes.apple.com/search?{params}",
                headers={"User-Agent": "TestsLLMLocaux/1.0"},
            )
            with urlopen(requete, timeout=15) as reponse:
                documents = json.load(reponse).get("results", [])

            lot: list[dict[str, str]] = []
            for doc in documents:
                titre = " ".join(str(doc.get("trackName") or "").split()).strip(" ;")
                if not titre or len(titre) > 75 or len(titre) < 3:
                    continue
                cle = titre.casefold()
                if cle in vus:
                    continue
                vus.add(cle)
                auteur = " ".join(str(doc.get("artistName") or "").split()).strip(" ;")
                libelle = f"{titre} ({auteur})" if auteur and len(titre) + len(auteur) < 72 else titre
                lot.append({"libelle": libelle, "genre_source": genre_indicatif})
                if len(lot) >= par_sujet:
                    break
            listes_par_sujet.append(lot)

        # On entrelace les sujets pour que chaque paquet de 64 livres mélange tous les genres.
        catalogue = []
        longueur_max = max((len(lot) for lot in listes_par_sujet), default=0)
        for indice in range(longueur_max):
            for lot in listes_par_sujet:
                if indice < len(lot):
                    catalogue.append(lot[indice])

        try:
            fichier_cache.write_text(
                json.dumps(catalogue, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    selection = catalogue[:nombre]
    return [
        [item["libelle"], f"APILIVRE{index + 1:05}.pdf", 1, 1]
        for index, item in enumerate(selection)
    ]


def categories_autorisees(libelle: str, regle_accessoire: bool = True) -> list[str]:
    """Liste des catégories permises pour un libellé : un livre numéroté n'est jamais un accessoire."""
    if regle_accessoire and MOTIF_LIVRE_NUMEROTE.search(libelle):
        return [categorie for categorie in CATEGORIES_INVENTAIRE if categorie != CATEGORIE_ACCESSOIRE]
    return list(CATEGORIES_INVENTAIRE)


def preparer_requete_bloc(
    lignes_bloc: Sequence[Sequence[Any]],
    regle_accessoire: bool = True,
) -> tuple[dict[str, Any], dict[str, Sequence[Any]]]:
    """Construit la requête `/api/chat` et le schéma JSON de catégorisation pour un paquet de lignes."""
    cles_lignes: dict[str, Sequence[Any]] = {
        f"L{index:02d}": ligne for index, ligne in enumerate(lignes_bloc, 1)
    }
    liste_titres = "\n".join(f"{cle} : {ligne[0]}" for cle, ligne in cles_lignes.items())
    categories_txt = ", ".join(CATEGORIES_INVENTAIRE)

    consigne_systeme = (
        f"Tu classes chaque article d'un inventaire de librairie dans une seule catégorie parmi : {categories_txt}. "
        "Pour chaque identifiant (L01, L02...), renvoie la catégorie correspondante en te basant sur l'auteur, "
        "l'œuvre connue ou les mots du titre."
    )
    if regle_accessoire:
        consigne_systeme += REGLE_ACCESSOIRE_V2_AMELIOREE

    messages = [
        {"role": "system", "content": consigne_systeme},
        {
            "role": "user",
            "content": f"Classe chacun de ces articles dans la bonne catégorie :\n{liste_titres}",
        },
    ]
    schema = {
        "type": "object",
        "required": list(cles_lignes),
        "additionalProperties": False,
        "properties": {
            cle: {"type": "string", "enum": categories_autorisees(str(ligne[0]), regle_accessoire)}
            for cle, ligne in cles_lignes.items()
        },
    }
    return {"messages": messages, "format": schema}, cles_lignes


def construire_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments en ligne de commande."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modele",
        default="qwen2.5:3b",
        help="Nom du modèle Ollama (par défaut : qwen2.5:3b)",
    )
    parser.add_argument(
        "--contexte",
        type=int,
        default=4096,
        metavar="TOKENS",
        help="Taille du contexte Ollama (par défaut : 4096)",
    )
    parser.add_argument(
        "--taille-bloc",
        type=int,
        default=64,
        metavar="LIGNES",
        help="Découpe l'inventaire en paquets de N lignes (par défaut : 64)",
    )
    parser.add_argument(
        "--livres-api",
        type=int,
        default=0,
        metavar="N",
        help="Ajoute N vrais titres de livres récupérés via API publique (avec cache local)",
    )
    parser.add_argument(
        "--rafraichir-api",
        action="store_true",
        help="Force le téléchargement depuis l'API publique en ignorant le cache local",
    )
    parser.add_argument(
        "--lignes-fictives",
        type=int,
        default=0,
        metavar="N",
        help="Ajoute N lignes fictives pour tester la montée en charge",
    )
    parser.add_argument(
        "--max-lignes",
        type=int,
        metavar="LIGNES",
        help="Limite le classement aux N premières lignes",
    )
    parser.add_argument(
        "--regle-accessoire",
        action="store_true",
        default=True,
        help="Active le prompt V2 amélioré avec la règle métier sur les accessoires (actif par défaut)",
    )
    parser.add_argument(
        "--ancien-prompt",
        action="store_true",
        help="Utilise l'ancien prompt V1 sans règle sur les accessoires (classe le marque-page The Witcher en Fantasy)",
    )
    parser.add_argument(
        "--sortie-csv",
        type=Path,
        metavar="FICHIER",
        help="Chemin d'un fichier CSV mis à jour progressivement à la fin de chaque paquet",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Affiche la sortie complète au format JSON",
    )
    parser.add_argument(
        "--mesures",
        action="store_true",
        help="Affiche les compteurs Ollama sur la sortie d'erreur",
    )
    return parser


def valider_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Vérifie la cohérence des bornes numériques passées en ligne de commande."""
    if args.contexte <= 0:
        parser.error("--contexte attend un nombre de tokens supérieur à zéro.")
    if args.taille_bloc is not None and args.taille_bloc <= 0:
        parser.error("--taille-bloc attend un nombre de lignes supérieur à zéro.")
    if args.livres_api < 0:
        parser.error("--livres-api attend un nombre positif ou nul.")
    if args.lignes_fictives < 0:
        parser.error("--lignes-fictives attend un nombre positif ou nul.")
    if args.max_lignes is not None and args.max_lignes <= 0:
        parser.error("--max-lignes attend un nombre de lignes supérieur à zéro.")


def main() -> None:
    """Point d'entrée du script de catégorisation par genre."""
    parser = construire_parser()
    args = parser.parse_args()
    valider_arguments(parser, args)

    try:
        lignes = lire_lignes_inventaire(CLASSEUR_DEFAUT)
    except (OSError, KeyError, ValueError) as erreur:
        raise SystemExit(f"Lecture du classeur impossible ({CLASSEUR_DEFAUT}) : {erreur}") from None

    if args.livres_api:
        try:
            lignes_api = recuperer_livres_api(args.livres_api, rafraichir=args.rafraichir_api)
        except (HTTPError, URLError, TimeoutError, ValueError) as erreur:
            raise SystemExit(f"Récupération de vrais livres via API impossible : {erreur}") from None
        lignes.extend(lignes_api)
        print(f"{len(lignes_api)} vrais livres ajoutés depuis l'API publique.", file=sys.stderr)

    if args.lignes_fictives:
        lignes.extend(generer_lignes_fictives(args.lignes_fictives))
        print(f"{args.lignes_fictives} lignes fictives ajoutées pour cet essai.", file=sys.stderr)

    if args.max_lignes is not None:
        lignes = lignes[: args.max_lignes]
        print(f"Classement limité aux {len(lignes)} premières lignes.", file=sys.stderr)

    taille_bloc = args.taille_bloc or max(len(lignes), 1)
    blocs = [lignes[index : index + taille_bloc] for index in range(0, len(lignes), taille_bloc)]

    totaux = {
        "blocs": len(blocs),
        "prompt_eval_count": 0,
        "eval_count": 0,
        "total_duration": 0,
        "load_duration": 0,
    }
    resultats_lignes: list[dict[str, Any]] = []

    fichier_csv = None
    writer_fichier = None
    if args.sortie_csv is not None:
        fichier_csv = args.sortie_csv.open("w", encoding="utf-8-sig", newline="")
        writer_fichier = csv.writer(fichier_csv, delimiter=";", lineterminator="\n")
        writer_fichier.writerow(COLONNES_INVENTAIRE_CATEGORISE)
        fichier_csv.flush()

    writer_stdout = None
    if not args.json:
        writer_stdout = csv.writer(sys.stdout, delimiter=";", lineterminator="\n")
        writer_stdout.writerow(COLONNES_INVENTAIRE_CATEGORISE)
        sys.stdout.flush()

    utiliser_regle_accessoire = bool(args.regle_accessoire and not args.ancien_prompt)

    try:
        for numero, lignes_bloc in enumerate(blocs, 1):
            est_dernier = numero == len(blocs)
            donnees: dict[str, Any] = {
                "model": args.modele,
                "stream": False,
                "keep_alive": 0 if est_dernier else "2m",
                "options": {
                    "temperature": 0,
                    "seed": 42,
                    "num_ctx": args.contexte,
                    "num_predict": max(300, len(lignes_bloc) * 15),
                },
            }
            requete_bloc, cles_lignes = preparer_requete_bloc(
                lignes_bloc,
                regle_accessoire=utiliser_regle_accessoire,
            )
            donnees.update(requete_bloc)

            try:
                resultat = appeler_ollama("chat", donnees, timeout=180)
            except HTTPError as erreur:
                raise SystemExit(f"Ollama a renvoyé une erreur HTTP {erreur.code}.") from None
            except (URLError, TimeoutError) as erreur:
                raise SystemExit(f"Ollama est injoignable : {erreur}") from None

            for cle in ("prompt_eval_count", "eval_count", "total_duration", "load_duration"):
                totaux[cle] += resultat.get(cle, 0)

            try:
                decisions = json.loads(resultat["message"]["content"])
            except (KeyError, ValueError) as erreur:
                raise SystemExit(f"Bloc {numero} : réponse JSON inexploitable ({erreur}).") from None

            lignes_csv_bloc: list[tuple[Any, ...]] = []
            for cle, (libelle, facture, page, article) in cles_lignes.items():
                categorie = decisions.get(cle, "Non classé")
                resultats_lignes.append(
                    {
                        "libelle": libelle,
                        "categorie": categorie,
                        "facture": facture,
                        "page": page,
                        "article": article,
                    }
                )
                lignes_csv_bloc.append((libelle, categorie, facture, page, article))

            if writer_fichier is not None and fichier_csv is not None:
                writer_fichier.writerows(lignes_csv_bloc)
                fichier_csv.flush()
            if writer_stdout is not None:
                writer_stdout.writerows(lignes_csv_bloc)
                sys.stdout.flush()

            debut = (numero - 1) * taille_bloc + 1
            fin = debut + len(lignes_bloc) - 1
            duree_s = round(resultat.get("total_duration", 0) / 1e9, 2)
            print(
                f"[Bloc {numero}/{len(blocs)} · lignes {debut}-{fin}] "
                f"{len(lignes_bloc)} articles classés en {duree_s}s",
                file=sys.stderr,
            )
    finally:
        if fichier_csv is not None:
            fichier_csv.close()

    if args.mesures:
        print(json.dumps(totaux), file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "modele": args.modele,
                    "regle_accessoire": utiliser_regle_accessoire,
                    "totaux": totaux,
                    "articles": resultats_lignes,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
