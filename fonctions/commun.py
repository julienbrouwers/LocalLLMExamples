"""Socle technique partagé pour les scripts d'évaluation des LLM locaux via Ollama.

Centralise :
- les chemins et constantes du projet ;
- la lecture de l'inventaire Excel et la sérialisation CSV ;
- la génération déterministe de lignes de livres fictives pour les tests de charge ;
- le client HTTP léger vers l'API REST locale d'Ollama (`/api/generate` et `/api/chat`).
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable, Sequence
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from openpyxl import load_workbook


# --- Chemins et configuration ---
RACINE: Path = Path(__file__).resolve().parent.parent
DOSSIER_ENTREES: Path = RACINE / "donnees" / "entrees"
DOSSIER_INVENTAIRES: Path = RACINE / "donnees" / "inventaires"
CLASSEUR_DEFAUT: Path = DOSSIER_INVENTAIRES / "inventaire-qwen-verification.xlsx"
CACHE_API_DEFAUT: Path = DOSSIER_ENTREES / "livres-openlibrary.json"
CSV_CATEGORISE_DEFAUT: Path = DOSSIER_INVENTAIRES / "inventaire-100-vrais-livres-categorise.csv"
OLLAMA_URL: str = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
# Délai minimal (secondes) imposé à chaque appel Ollama ; utile pour Mistral 7B sur 4 Go de VRAM.
OLLAMA_TIMEOUT_MIN: int = int(os.environ.get("OLLAMA_TIMEOUT", "0") or 0)

# --- Schéma des données ---
COLONNES_INVENTAIRE: tuple[str, ...] = ("Libellé", "Facture", "Page", "Article")
COLONNES_INVENTAIRE_CATEGORISE: tuple[str, ...] = ("Libellé", "Catégorie", "Facture", "Page", "Article")

CATEGORIES_INVENTAIRE: tuple[str, ...] = (
    "Super-héros / Action",
    "Science-fiction / Horreur",
    "Fantasy",
    "Aventure / Exploration",
    "Accessoire / Papeterie",
)
CATEGORIES_LIVRES: tuple[str, ...] = (
    "Science-fiction / Horreur",
    "Super-héros / Action",
    "Fantasy",
    "Aventure / Exploration",
)

CLES_MESURES_OLLAMA: tuple[str, ...] = (
    "prompt_eval_count",
    "eval_count",
    "done_reason",
    "total_duration",
    "load_duration",
)

# --- Fixtures déterministes pour les tests de montée en charge ---
# Fixtures V1 (historisées) : 8 destinations toujours préfixées par « Chroniques des {destination} Tome {tome} »
# -> créaient un contraste artificiel sur le mot « Volume » (présent uniquement dans « Aliens Volume 2 Omnibus »).
# Fixtures V2 améliorées (actives par défaut) : 20 collections de livres variées.
COLLECTIONS_FICTIVES: tuple[str, ...] = (
    "Chroniques des Marées",
    "L'Empire de Corail",
    "Les Veilleurs du Bastion",
    "Le Codex d'Obsidienne",
    "Brigade Boréale",
    "Mémoires d'Orion",
    "Les Éclaireurs du Levant",
    "Légendes de Hurlevent",
    "Escadron Sirius",
    "Archives du Grand Nord",
    "La Confrérie des Brumes",
    "Carnets de la Citadelle",
    "Les Sentinelles de Quartz",
    "Histoires du Vieux Phare",
    "Le Cercle des Tempêtes",
    "Sagas de la Vallée Haute",
    "Les Passeurs d'Horizons",
    "Contes de la Rive Ouest",
    "La Garde de Nébuleuse",
    "Recueil des Montagnes Grises",
)

SOUS_TITRES_FICTIFS: tuple[str, ...] = (
    "Dernier voyage",
    "La cité perdue",
    "Le passage secret",
    "L'aube oubliée",
    "Le phare éteint",
    "La forêt rouge",
    "Les portes du nord",
    "Le serment brisé",
    "L'ombre du rempart",
    "Les cendres de l'hiver",
    "La piste des glaces",
    "Le bastion silencieux",
    "L'archipel de fer",
)


def lire_lignes_inventaire(chemin_classeur: Path = CLASSEUR_DEFAUT) -> list[list[Any]]:
    """Lit l'onglet « Inventaire » du classeur Excel et renvoie les lignes utiles."""
    classeur = load_workbook(chemin_classeur, read_only=True, data_only=True)
    try:
        lignes = classeur["Inventaire"].values
        entete = next(lignes, ())
        indices = [entete.index(colonne) for colonne in COLONNES_INVENTAIRE]
        return [[ligne[index] for index in indices] for ligne in lignes]
    finally:
        classeur.close()


def formater_csv(
    lignes: Iterable[Sequence[Any]],
    entete: Sequence[str] = COLONNES_INVENTAIRE,
) -> str:
    """Sérialise une séquence de lignes en texte CSV délimité par des points-virgules."""
    tampon = StringIO(newline="")
    writer = csv.writer(tampon, delimiter=";", lineterminator="\n")
    writer.writerow(entete)
    writer.writerows(lignes)
    return tampon.getvalue()


def lire_tableau_csv(chemin_classeur: Path = CLASSEUR_DEFAUT) -> str:
    """Convertit l'inventaire Excel en texte CSV prêt à être injecté dans un prompt."""
    return formater_csv(lire_lignes_inventaire(chemin_classeur))


def generer_lignes_fictives(nombre: int) -> list[list[Any]]:
    """Génère `nombre` lignes d'inventaire fictives et déterministes pour les tests de volume."""
    nb_collections = len(COLLECTIONS_FICTIVES)
    nb_sous_titres = len(SOUS_TITRES_FICTIFS)
    lignes: list[list[Any]] = []

    for index in range(nombre):
        collection = COLLECTIONS_FICTIVES[index % nb_collections]
        indice_sous_titre = (index * 3 + index // nb_collections) % nb_sous_titres
        sous_titre = SOUS_TITRES_FICTIFS[indice_sous_titre]
        tome = index // nb_collections + 1
        libelle = f"{collection} Tome {tome} : {sous_titre}"
        lignes.append([libelle, f"FICTIF{index + 1:05}.pdf", 1, 1])

    return lignes


def ajouter_lignes_fictives_csv(tableau_csv: str, nombre: int) -> str:
    """Ajoute `nombre` lignes fictives à la suite d'un tableau CSV existant."""
    tampon = StringIO(newline="")
    writer = csv.writer(tampon, delimiter=";", lineterminator="\n")
    writer.writerows(generer_lignes_fictives(nombre))
    suffixe = "\n" if not tableau_csv.endswith("\n") else ""
    return tableau_csv + suffixe + tampon.getvalue()


def appeler_ollama(endpoint: str, donnees: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    """Envoie une requête POST JSON à l'API locale d'Ollama et renvoie la réponse décodée."""
    cible = endpoint.lstrip("/")
    requete = Request(
        f"{OLLAMA_URL}/api/{cible}",
        data=json.dumps(donnees, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(requete, timeout=max(timeout, OLLAMA_TIMEOUT_MIN)) as reponse:
        return json.load(reponse)


def decharger_modele(modele: str) -> None:
    """Demande à Ollama de libérer immédiatement le modèle de la VRAM (`keep_alive: 0`)."""
    try:
        appeler_ollama("generate", {"model": modele, "keep_alive": 0}, timeout=10)
    except (HTTPError, URLError, TimeoutError, OSError):
        pass


def extraire_mesures(resultat: dict[str, Any]) -> dict[str, Any]:
    """Extrait les compteurs de performance renvoyés par Ollama."""
    return {cle: resultat[cle] for cle in CLES_MESURES_OLLAMA if cle in resultat}
