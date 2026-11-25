"""Propose des recommandations de livres par genre à partir du CSV catégorisé avec un modèle local Ollama."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from commun import (
    CACHE_API_DEFAUT,
    CATEGORIES_LIVRES,
    CSV_CATEGORISE_DEFAUT,
    appeler_ollama,
    extraire_mesures,
)


# --- Historique des prompts ---
# Prompt V1 (historisé) : demande « de mémoire » sans catalogue de candidats, avec un tableau JSON ouvert.
# Limites observées sur Qwen 2.5 3B :
# - Avec un schéma JSON `"type": "array"`, renvoie une liste vide `[]` dès que la liste d'entrée atteint 21 livres.
# - Avec 3 clés obligatoires mais sans catalogue (`--sans-catalogue`), invente des romans fictifs
#   (« Le Masque de Zéphyr », « La Terre des morts ») ou recommande des livres déjà possédés (« Dune - Tome 1 »).
SYSTEME_V1_MEMOIRE: str = (
    "Tu es un libraire francophone expert. Tu analyses les livres qu'un lecteur possède déjà dans un genre donné "
    "pour lui conseiller ses prochains achats en français.\n"
    "Règles strictes :\n"
    "1. Ne recommande JAMAIS un titre qui figure déjà dans sa liste.\n"
    "2. Si le lecteur possède déjà les tomes 2 ou 3 d'une série sans avoir le Tome 1, propose ce Tome 1 manquant.\n"
    "3. Propose des œuvres réelles et connues du même genre avec une phrase d'explication."
)

# Prompt V2 amélioré (actif par défaut) : ancré sur un catalogue de vrais candidats non possédés
# (filtrés par Python depuis l'API + détection des Tome 1 manquants) et clés JSON obligatoires `recommandation_1..N`.
SYSTEME_V2_CATALOGUE: str = (
    "Tu es un libraire francophone expert. Tu compares les livres déjà possédés par un lecteur "
    "avec un catalogue de vrais livres disponibles qu'il ne possède pas encore.\n"
    "Règles strictes :\n"
    "1. Choisis chaque recommandation UNIQUEMENT parmi la liste « Catalogue de livres disponibles ».\n"
    "2. Choisis deux livres différents d'une recommandation à l'autre.\n"
    "3. Si un Tome 1 manquant d'une série déjà commencée par le lecteur figure dans le catalogue "
    ", sélectionne-le en priorité en recommandation_1.\n"
    "4. Explique en une phrase le lien précis entre le livre choisi et les titres de sa collection."
)

CATALOGUE_SECOURS: dict[str, tuple[str, ...]] = {
    "Science-fiction / Horreur": (
        "Fondation (Isaac Asimov)",
        "Les Montagnes hallucinées (H. P. Lovecraft)",
        "Hypérion (Dan Simmons)",
        "Ubik (Philip K. Dick)",
        "La Nuit des temps (René Barjavel)",
        "Je suis une légende (Richard Matheson)",
        "Shining (Stephen King)",
        "Neuromancien (William Gibson)",
    ),
    "Super-héros / Action": (
        "Batman : Année Un (Frank Miller & David Mazzucchelli)",
        "Watchmen (Alan Moore & Dave Gibbons)",
        "Daredevil : Born Again (Frank Miller)",
        "Spider-Man : La Dernière Chasse de Kraven (J. M. DeMatteis)",
        "Invincible Intégrale Tome 1 (Robert Kirkman)",
        "X-Men : Dieu crée, l'homme détruit (Chris Claremont)",
    ),
    "Fantasy": (
        "Le Nom du vent (Patrick Rothfuss)",
        "Le Sorceleur - Tome 1 : Le Dernier Vœu (Andrzej Sapkowski)",
        "Les Salauds Gentilshommes - Tome 1 : Les Mensonges de Locke Lamora (Scott Lynch)",
        "La Horde du Contrevent (Alain Damasio)",
        "Gagner la guerre (Jean-Philippe Jaworski)",
        "L'Empire des tempêtes (Jon Skovron)",
    ),
    "Aventure / Exploration": (
        "Vingt mille lieues sous les mers (Jules Verne)",
        "L'Île au trésor (Robert Louis Stevenson)",
        "Le Comte de Monte-Cristo (Alexandre Dumas)",
        "L'Appel de la forêt (Jack London)",
        "Michel Strogoff (Jules Verne)",
        "Croc-Blanc (Jack London)",
    ),
}


def charger_collection(chemin_csv: Path, seulement_pdf: bool = False) -> list[dict[str, str]]:
    """Charge le fichier CSV catégorisé (et filtre optionnellement les seuls achats PDF `FA*.pdf`)."""
    if not chemin_csv.exists():
        raise FileNotFoundError(
            f"Fichier {chemin_csv} introuvable. Lance d'abord categoriser_livres.py avec --sortie-csv."
        )
    with chemin_csv.open("r", encoding="utf-8-sig", newline="") as flux:
        lecteur = csv.DictReader(flux, delimiter=";")
        lignes: list[dict[str, str]] = []
        for ligne in lecteur:
            if seulement_pdf and not str(ligne.get("Facture", "")).startswith("FA"):
                continue
            lignes.append(ligne)
    return lignes


def choisir_genre(lignes: Sequence[dict[str, str]], saisie_genre: str | None = None) -> str:
    """Affiche les genres disponibles dans la collection et résout le choix de l'utilisateur."""
    compteurs: dict[str, int] = {}
    for ligne in lignes:
        categorie = ligne.get("Catégorie", "").strip()
        if categorie and categorie != "Accessoire / Papeterie":
            compteurs[categorie] = compteurs.get(categorie, 0) + 1

    genres_dispo = [genre for genre in CATEGORIES_LIVRES if genre in compteurs] + [
        genre for genre in compteurs if genre not in CATEGORIES_LIVRES
    ]
    if not genres_dispo:
        raise ValueError("Aucun livre catégorisé trouvé dans le fichier CSV.")

    if not saisie_genre:
        print("Genres disponibles dans ta collection :", file=sys.stderr)
        for index, genre in enumerate(genres_dispo, 1):
            print(f"  {index}. {genre} ({compteurs[genre]} livre(s))", file=sys.stderr)
        saisie_genre = input("Quel genre veux-tu explorer (numéro ou nom) ? ").lstrip("﻿").strip()

    if not saisie_genre:
        raise SystemExit("Saisis un numéro ou un nom de genre.")

    if saisie_genre.isdigit():
        indice = int(saisie_genre) - 1
        if 0 <= indice < len(genres_dispo):
            return genres_dispo[indice]

    cible = saisie_genre.casefold()
    for genre in genres_dispo:
        if cible in genre.casefold():
            return genre

    raise SystemExit(f"Genre « {saisie_genre} » introuvable parmi : {', '.join(genres_dispo)}")


def normaliser_titre(texte: str) -> str:
    """Réduit un titre à sa racine alphanumérique en minuscules pour détecter les doublons."""
    base = str(texte).split("(")[0].split(":")[0].split("-")[0]
    return "".join(caractere for caractere in base.casefold() if caractere.isalnum())


def obtenir_candidats_reels(titres_possedes: Sequence[str], genre: str) -> list[str]:
    """Construit une liste de vrais livres candidats non possédés (catalogue API + tomes 1 manquants)."""
    norm_possedes = {normaliser_titre(titre) for titre in titres_possedes}
    texte_possedes = " | ".join(titres_possedes).casefold()
    candidats: list[str] = []

    # Détection de séries dont le lecteur possède un Tome 2 ou 3 sans le Tome 1
    if "alien tome 2" in texte_possedes and "alien tome 1" not in texte_possedes:
        candidats.append("Alien Tome 1 : Lignes de sang (Phillip Kennedy Johnson)")
    if "tmnt tome 17" in texte_possedes and "tmnt tome 1" not in texte_possedes:
        candidats.append("Les Tortues Ninja - TMNT Tome 1 : Changement constant (Tom Waltz & Kevin Eastman)")

    # Ajout de vrais livres du même genre depuis le cache API (ou catalogue de référence)
    if CACHE_API_DEFAUT.exists():
        try:
            catalogue = json.loads(CACHE_API_DEFAUT.read_text(encoding="utf-8"))
            for item in catalogue:
                if item.get("genre_source") == genre:
                    libelle = item.get("libelle", "").strip()
                    if libelle and normaliser_titre(libelle) not in norm_possedes and libelle not in candidats:
                        candidats.append(libelle)
        except (OSError, ValueError):
            pass

    for libelle in CATALOGUE_SECOURS.get(genre, ()):
        if normaliser_titre(libelle) not in norm_possedes and libelle not in candidats:
            candidats.append(libelle)

    return candidats[:18]


def preparer_requete_recommandation(
    titres_possedes: Sequence[str],
    genre: str,
    nombre: int,
    sans_catalogue: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Construit la requête `/api/chat` et le schéma JSON contraint pour `nombre` recommandations."""
    liste_titres = "\n".join(f"- {titre}" for titre in titres_possedes)
    cles_rec = [f"recommandation_{index}" for index in range(1, nombre + 1)]

    if sans_catalogue:
        systeme = SYSTEME_V1_MEMOIRE
        utilisateur = (
            f"Genre choisi : {genre}\n"
            f"Livres déjà présents dans ma collection ({len(titres_possedes)}) :\n{liste_titres}\n\n"
            f"Remplis les {nombre} recommandations ({', '.join(cles_rec)}) "
            "avec des livres que je ne possède pas encore."
        )
        titre_schema: dict[str, Any] = {"type": "string"}
    else:
        candidats = obtenir_candidats_reels(titres_possedes, genre)
        liste_candidats = "\n".join(f"- {candidat}" for candidat in candidats)
        systeme = SYSTEME_V2_CATALOGUE
        utilisateur = (
            f"Genre choisi : {genre}\n"
            f"Livres déjà présents dans ma collection ({len(titres_possedes)}) :\n{liste_titres}\n\n"
            f"Catalogue de livres disponibles (non possédés) :\n{liste_candidats}\n\n"
            f"Sélectionne les {nombre} meilleures recommandations ({', '.join(cles_rec)}) dans ce catalogue."
        )
        titre_schema = {"type": "string", "enum": candidats}

    item_schema = {
        "type": "object",
        "required": ["titre", "lien_collection", "raison"],
        "additionalProperties": False,
        "properties": {
            "titre": titre_schema,
            "lien_collection": {"type": "string"},
            "raison": {"type": "string"},
        },
    }
    schema = {
        "type": "object",
        "required": cles_rec,
        "additionalProperties": False,
        "properties": {cle: item_schema for cle in cles_rec},
    }
    messages = [
        {"role": "system", "content": systeme},
        {"role": "user", "content": utilisateur},
    ]
    return {"messages": messages, "format": schema}, cles_rec


def filtrer_recommandations(
    recommandations_brutes: Sequence[dict[str, Any]],
    titres_genre: Sequence[str],
    nombre: int,
) -> list[dict[str, str]]:
    """Écarte les doublons ou les titres déjà possédés à l'identique dans la collection."""
    possedes_minuscules = {titre.casefold() for titre in titres_genre}
    recommandations: list[dict[str, str]] = []
    deja_vus: set[str] = set()

    for rec in recommandations_brutes:
        titre = str(rec.get("titre", "")).strip()
        cle = titre.casefold()
        if not titre or cle in possedes_minuscules or cle in deja_vus:
            continue
        deja_vus.add(cle)
        recommandations.append(
            {
                "titre": titre,
                "lien_collection": str(rec.get("lien_collection", "")).strip(),
                "raison": str(rec.get("raison", "")).strip(),
            }
        )
        if len(recommandations) >= nombre:
            break

    return recommandations


def afficher_recommandations(
    recommandations: Sequence[dict[str, str]],
    genre: str,
    nb_livres_genre: int,
) -> None:
    """Affiche les recommandations de lecture formatées en texte lisible."""
    print(f"\nRecommandations « {genre} » (basées sur tes {nb_livres_genre} livres de ce genre) :\n")
    for index, rec in enumerate(recommandations, 1):
        print(f"{index}. {rec['titre']}")
        print(f"   Lien avec ta collection : {rec['lien_collection']}")
        print(f"   Pourquoi : {rec['raison']}\n")


def construire_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments en ligne de commande."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modele",
        default="qwen2.5:3b",
        help="Nom du modèle Ollama (par défaut : qwen2.5:3b)",
    )
    parser.add_argument(
        "--fichier-csv",
        type=Path,
        default=CSV_CATEGORISE_DEFAUT,
        help="Fichier CSV catégorisé à analyser",
    )
    parser.add_argument(
        "--genre",
        help="Nom ou numéro du genre (demandé au clavier si omis)",
    )
    parser.add_argument(
        "--nombre",
        type=int,
        default=3,
        help="Nombre de recommandations souhaitées (par défaut : 3)",
    )
    parser.add_argument(
        "--seulement-pdf",
        action="store_true",
        help="Se base uniquement sur les 8 achats réels issus des factures PDF (FA*.pdf)",
    )
    parser.add_argument(
        "--sans-catalogue",
        action="store_true",
        help="Laisse le LLM proposer des titres de mémoire (sans catalogue de candidats vérifiés)",
    )
    parser.add_argument(
        "--contexte",
        type=int,
        default=4096,
        metavar="TOKENS",
        help="Taille du contexte Ollama (par défaut : 4096)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Affiche la sortie au format JSON",
    )
    parser.add_argument(
        "--mesures",
        action="store_true",
        help="Affiche les compteurs Ollama sur la sortie d'erreur",
    )
    return parser


def main() -> None:
    """Point d'entrée du script de recommandation de livres par genre."""
    parser = construire_parser()
    args = parser.parse_args()

    if args.nombre <= 0:
        parser.error("--nombre attend un entier supérieur à zéro.")

    try:
        lignes = charger_collection(args.fichier_csv, seulement_pdf=args.seulement_pdf)
    except (OSError, ValueError) as erreur:
        raise SystemExit(str(erreur)) from None

    genre_retenu = choisir_genre(lignes, args.genre)

    # Python filtre instantanément la collection pour n'envoyer au LLM que les livres du genre choisi.
    titres_genre = list(
        dict.fromkeys(
            ligne["Libellé"].strip()
            for ligne in lignes
            if ligne.get("Catégorie", "").strip() == genre_retenu and ligne.get("Libellé", "").strip()
        )
    )
    print(
        f"Filtrage Python : {len(titres_genre)} livre(s) du genre « {genre_retenu} » envoyés à {args.modele} "
        f"(sur {len(lignes)} articles au total).",
        file=sys.stderr,
    )

    donnees: dict[str, Any] = {
        "model": args.modele,
        "stream": False,
        "keep_alive": 0,
        "options": {
            "temperature": 0,
            "seed": 42,
            "num_ctx": args.contexte,
            "num_predict": 450,
        },
    }
    requete_rec, cles_rec = preparer_requete_recommandation(
        titres_genre,
        genre_retenu,
        args.nombre,
        sans_catalogue=args.sans_catalogue,
    )
    donnees.update(requete_rec)

    try:
        resultat = appeler_ollama("chat", donnees, timeout=180)
    except HTTPError as erreur:
        raise SystemExit(f"Ollama a renvoyé une erreur HTTP {erreur.code}.") from None
    except (URLError, TimeoutError) as erreur:
        raise SystemExit(f"Ollama est injoignable : {erreur}") from None

    if args.mesures:
        print(json.dumps(extraire_mesures(resultat)), file=sys.stderr)

    try:
        objet_json = json.loads(resultat["message"]["content"])
        recommandations_brutes = [objet_json[cle] for cle in cles_rec if cle in objet_json]
    except (KeyError, ValueError) as erreur:
        raise SystemExit(f"Réponse JSON inexploitable : {erreur}") from None

    recommandations = filtrer_recommandations(recommandations_brutes, titres_genre, args.nombre)

    if args.json:
        print(
            json.dumps(
                {
                    "modele": args.modele,
                    "genre": genre_retenu,
                    "livres_collection_genre": titres_genre,
                    "recommandations": recommandations,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    afficher_recommandations(recommandations, genre_retenu, len(titres_genre))


if __name__ == "__main__":
    main()
