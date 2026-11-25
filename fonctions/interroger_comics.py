"""Recherche un titre ou un mot-clé dans l'inventaire de livres avec un modèle local Ollama.

Prend en charge deux modes d'évaluation :
- appel unitaire (tableau CSV complet transmis en une seule requête) ;
- découpage par paquets (`--taille-bloc 64`) avec conversation réinitialisée entre deux blocs
  et format d'entrée/sortie adapté à chaque famille de modèle (`qwen2.5:3b`, `llama3.2:3b`, `mistral:7b`).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from commun import (
    CLASSEUR_DEFAUT,
    ajouter_lignes_fictives_csv,
    appeler_ollama,
    decharger_modele,
    extraire_mesures,
    formater_csv,
    lire_tableau_csv,
)


# --- Historique des prompts ---
# Prompt Qwen V1 (historisé) : citait uniquement « Parapluies rouges ».
# Limite observée : sur 64 lignes avec les 20 collections de livres variées, Qwen oubliait le pluriel en tête
# de titre (« Aliens Volume 2 Omnibus » -> 2/3).
EXEMPLE_QWEN_V1_HISTORIQUE: str = (
    "Exemple : pour 'pluie', le titre 'Parapluies rouges' correspond car il contient 'pluie'. "
    "Le titre 'Vent du nord' ne correspond pas."
)

# Prompt Qwen V2 amélioré (actif par défaut) : montre le singulier ET le pluriel en tête de titre
# ('Pluie Tome 2', 'Pluies Volume 2' et 'Parapluies rouges'), ce qui stabilise le 3/3 à 64 lignes variées.
EXEMPLE_QWEN_V2_AMELIORE: str = (
    "Exemple : pour 'pluie', les titres 'Pluie Tome 2', 'Pluies Volume 2' et 'Parapluies rouges' "
    "correspondent car ils contiennent 'pluie'. Le titre 'Vent du nord' ne correspond pas."
)

SYSTEME_QWEN_RECHERCHE: str = "Tu recherches une suite de caractères dans la première colonne d'un tableau."


# --- Fonctions de préparation et de filtrage du tableau CSV ---
def lire_tableau() -> str:
    """Lit le classeur Excel par défaut et renvoie le tableau CSV à 4 colonnes."""
    return lire_tableau_csv(CLASSEUR_DEFAUT)


def ajouter_lignes_fictives(tableau: str, nombre: int) -> str:
    """Ajoute `nombre` lignes de livres fictives variées au tableau CSV."""
    return ajouter_lignes_fictives_csv(tableau, nombre)


def limiter_lignes(tableau: str, nombre: int) -> str:
    """Tronque le tableau CSV pour ne conserver que l'en-tête et les `nombre` premières lignes."""
    lignes = tableau.splitlines(keepends=True)
    if not lignes:
        return tableau
    return "".join(lignes[: nombre + 1])


def limiter_resultats_texte(contenu: str, tableau: str, limite: int) -> str:
    """Plafonne à `limite` le nombre de lignes CSV correspondantes extraites d'une réponse texte."""
    lignes_csv = set(tableau.strip().splitlines()[1:])
    correspondances: list[str] = []

    for ligne in contenu.splitlines():
        ligne_propre = ligne.strip()
        if ligne_propre in lignes_csv and ligne_propre not in correspondances:
            correspondances.append(ligne_propre)
            if len(correspondances) >= limite:
                break

    if not correspondances:
        return contenu
    return "\n".join(correspondances)


def construire_prompt_qwen(tableau: str, terme: str) -> str:
    """Construit le prompt de recherche CSV avec exemple hors-domaine pour Qwen 2.5 3B."""
    return (
        f"{EXEMPLE_QWEN_V2_AMELIORE}\n"
        f"Critère pour mon tableau réel : titre contient '{terme}'.\n"
        "Compare en minuscules. Examine chaque titre, même si le terme est à l'intérieur d'un mot. "
        "Quand le titre correspond, recopie la ligne entière. Si le titre ne correspond pas, saute la ligne.\n"
        "Tableau réel :\n"
        f"{tableau}\n"
        f"Affiche les lignes du tableau réel dont le titre contient '{terme}'."
    )


# --- Consignes adaptées par modèle (mode unitaire) ---
def preparer_mistral(tableau: str, terme: str) -> dict[str, Any]:
    """Construit la requête `/api/chat` avec schéma JSON `enum` pour Mistral 7B (mode unitaire)."""
    exemple = (
        "CSV fictif :\nLibellé;Facture;Page;Article\n"
        "Arbres du Nord;EXEMPLE.pdf;1;1\nForêt profonde;EXEMPLE.pdf;1;2\n"
        "L’Arbre bleu;EXEMPLE.pdf;1;3\nMot : arbre"
    )
    lignes_exemple = ["Arbres du Nord;EXEMPLE.pdf;1;1", "L’Arbre bleu;EXEMPLE.pdf;1;3"]

    messages = [
        {
            "role": "system",
            "content": (
                "Tu effectues un filtre textuel dans un CSV. La première colonne est Libellé. "
                "Compare uniquement les caractères du libellé, en ignorant la casse. "
                "Renvoie les lignes d'origine dans l'ordre sous la clé JSON lignes."
            ),
        },
        {"role": "user", "content": exemple},
        {
            "role": "assistant",
            "content": json.dumps({"lignes": lignes_exemple}, ensure_ascii=False, separators=(",", ":")),
        },
        {
            "role": "user",
            "content": (
                f"CSV réel :\n{tableau}\nMot : {terme}\n"
                f"Chaque ligne retenue doit contenir « {terme} » dans son libellé. "
                "Donne uniquement le JSON avec la clé lignes."
            ),
        },
    ]
    schema = {
        "type": "object",
        "required": ["lignes"],
        "additionalProperties": False,
        "properties": {
            "lignes": {
                "type": "array",
                "items": {"type": "string", "enum": tableau.strip().splitlines()[1:]},
            }
        },
    }
    return {"messages": messages, "format": schema}


def preparer_llama(tableau: str, terme: str) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Construit la requête `/api/chat` caractère par caractère avec verdict booléen pour Llama 3.2 3B."""
    def espacer_lettres(texte: str) -> str:
        return " ".join(texte.lower().replace(" ", "␠"))

    lecteur = csv.reader(StringIO(tableau), delimiter=";")
    next(lecteur, None)
    lignes = {f"R{index:02}": ligne for index, ligne in enumerate(lecteur, 1)}
    titres = "\n".join(f"{identifiant} : {espacer_lettres(ligne[0])}" for identifiant, ligne in lignes.items())
    chaine = espacer_lettres(terme)

    messages = [
        {
            "role": "system",
            "content": (
                "Tu classes chacune des lignes. Pour chaque identifiant, réponds true si le titre "
                "contient le terme, false sinon. Compare les lettres côte à côte, sans interpréter le sens. "
                "Les caractères de chaque titre sont séparés par des espaces pour faciliter la comparaison. "
                "La séquence doit apparaître sans autre caractère intercalé. Il peut rester des caractères "
                "avant et après. Renvoie un objet JSON avec un booléen par identifiant."
            ),
        },
        {
            "role": "user",
            "content": (
                "Mot : r o b o t\nTitres :\n"
                "X : r o b o t s ␠ r o u g e s\n"
                "Y : m o n ␠ r o b o t\n"
                "Z : a n d r o ï d e s"
            ),
        },
        {"role": "assistant", "content": '{"X":true,"Y":true,"Z":false}'},
        {
            "role": "user",
            "content": (
                f"Mot : {chaine}\nTitres en minuscules :\n{titres}\n"
                f"Renvoie true ou false pour chaque identifiant, selon que son titre contient « {chaine} »."
            ),
        },
    ]
    schema = {
        "type": "object",
        "required": list(lignes),
        "additionalProperties": False,
        "properties": {identifiant: {"type": "boolean"} for identifiant in lignes},
    }
    return {"messages": messages, "format": schema}, lignes


def afficher_llama(
    contenu: str,
    lignes: dict[str, list[str]],
    max_resultats: int | None = None,
) -> None:
    """Valide les verdicts booléens de Llama et affiche les lignes CSV retenues."""
    choix = json.loads(contenu)
    if (
        not isinstance(choix, dict)
        or choix.keys() != lignes.keys()
        or any(type(valeur) is not bool for valeur in choix.values())
    ):
        raise ValueError("Llama doit donner une décision booléenne pour chaque ligne.")

    correspondances = [ligne for identifiant, ligne in lignes.items() if choix[identifiant]]
    if max_resultats is not None:
        correspondances = correspondances[:max_resultats]

    if not correspondances:
        print("Aucune correspondance trouvée par Llama.")
        return

    writer = csv.writer(sys.stdout, delimiter=";", lineterminator="\n")
    writer.writerows(correspondances)


# --- Mode découpage par paquets (--taille-bloc) ---
def extraire_lignes_bloc_qwen(contenu: str, lignes_bloc: Sequence[Sequence[Any]]) -> list[list[Any]]:
    """Filtre la réponse texte de Qwen pour ne retenir que les lignes exactes présentes dans le bloc."""
    # Qwen recopie tantôt la ligne CSV entière, tantôt le titre seul : on accepte les deux formes.
    attendues: dict[str, list[Any]] = {}
    for ligne in lignes_bloc:
        attendues[";".join(str(col) for col in ligne)] = list(ligne)
        attendues.setdefault(str(ligne[0]), list(ligne))
    trouvees: list[list[Any]] = []
    vues: set[str] = set()

    for ligne_brute in contenu.splitlines():
        candidate = ligne_brute.strip().lstrip("*-•0123456789. ").strip("` ")
        ligne = attendues.get(candidate)
        if ligne is not None and ligne[0] not in vues:
            vues.add(ligne[0])
            trouvees.append(ligne)

    return trouvees


def preparer_bloc_titres_json(lignes_bloc: Sequence[Sequence[Any]], terme: str) -> dict[str, Any]:
    """Construit la requête par bloc (liste de titres -> JSON `{"titres": [...]}`) pour Llama et Mistral.

    L'exemple négatif (`{"titres":[]}`) est placé en premier et l'exemple positif en second afin d'éviter
    le biais de récence qui pousserait le modèle à répondre systématiquement `[]` sur tous les blocs.
    """
    titres = list(dict.fromkeys(str(ligne[0]) for ligne in lignes_bloc))
    liste = "\n".join(f"- {titre}" for titre in titres)

    messages = [
        {
            "role": "system",
            "content": (
                "Tu filtres une liste de titres. Compare uniquement les caractères en minuscules, "
                "sans interpréter le sens. Retiens uniquement les titres qui contiennent la suite "
                "exacte de caractères demandée (même à l'intérieur d'un mot ou au pluriel). "
                "Renvoie un objet JSON {\"titres\": [...]}. Si aucun titre ne contient la chaîne, "
                "renvoie {\"titres\": []}."
            ),
        },
        {
            "role": "user",
            "content": "Mot : pluie\nTitres :\n- Soleil couchant\n- Nuages d'orage\n- Forêt profonde",
        },
        {"role": "assistant", "content": '{"titres":[]}'},
        {
            "role": "user",
            "content": "Mot : pluie\nTitres :\n- Parapluies rouges\n- Vent du nord\n- Pluie d'automne",
        },
        {"role": "assistant", "content": '{"titres":["Parapluies rouges","Pluie d\'automne"]}'},
        {
            "role": "user",
            "content": f'Mot : {terme}\nTitres :\n{liste}\nQuels titres contiennent la chaîne "{terme}" ?',
        },
    ]
    schema = {
        "type": "object",
        "required": ["titres"],
        "additionalProperties": False,
        "properties": {"titres": {"type": "array", "items": {"type": "string"}}},
    }
    return {"messages": messages, "format": schema}


def executer_par_blocs(args: argparse.Namespace, tableau: str, terme: str) -> None:
    """Découpe le CSV en paquets de N lignes et interroge Ollama paquet par paquet sans historique."""
    lecteur = csv.reader(StringIO(tableau), delimiter=";")
    next(lecteur, None)
    lignes = list(lecteur)
    taille_bloc = args.taille_bloc or max(len(lignes), 1)
    blocs = [lignes[index : index + taille_bloc] for index in range(0, len(lignes), taille_bloc)]

    mistral = args.modele in ("mistral:7b", "mistral") and not args.consigne_commune
    llama = args.modele == "llama3.2:3b" and not args.consigne_commune

    toutes_correspondances: list[list[Any]] = []
    rapport_blocs: list[dict[str, Any]] = []
    totaux = {
        "blocs": len(blocs),
        "prompt_eval_count": 0,
        "eval_count": 0,
        "total_duration": 0,
        "load_duration": 0,
    }
    writer_direct = csv.writer(sys.stdout, delimiter=";", lineterminator="\n")

    for numero, lignes_bloc in enumerate(blocs, 1):
        est_dernier = numero == len(blocs)
        tableau_bloc = formater_csv(lignes_bloc)
        options: dict[str, Any] = {
            "temperature": 0,
            "seed": 42,
            "num_predict": 200 if (mistral or llama) else 300,
        }
        # repeat_penalty évite les boucles sur Llama 3.2 3B, mais pénaliserait la répétition
        # du préfixe « Alien Tome » sur Mistral 7B et Qwen 2.5 3B.
        if llama:
            options["repeat_penalty"] = 1.15
        if args.contexte is not None:
            options["num_ctx"] = args.contexte

        donnees: dict[str, Any] = {
            "model": args.modele,
            "stream": False,
            "keep_alive": 0 if est_dernier else "2m",
            "options": options,
        }

        if mistral or llama:
            endpoint = "chat"
            donnees.update(preparer_bloc_titres_json(lignes_bloc, terme))
        else:
            endpoint = "generate"
            donnees.update(
                {
                    "system": SYSTEME_QWEN_RECHERCHE,
                    "prompt": construire_prompt_qwen(tableau_bloc, terme),
                }
            )

        resultat = appeler_ollama(endpoint, donnees, timeout=180)
        for cle in ("prompt_eval_count", "eval_count", "total_duration", "load_duration"):
            totaux[cle] += resultat.get(cle, 0)

        erreur_bloc: str | None = None
        trouvees_bloc: list[list[Any]] = []
        if resultat.get("done_reason") == "length":
            erreur_bloc = "sortie_coupee"
        else:
            try:
                if llama or mistral:
                    titres_bruts = json.loads(resultat["message"]["content"]).get("titres", [])
                    if not isinstance(titres_bruts, list):
                        erreur_bloc = "decision_invalide"
                    else:
                        titres_retenus = {titre for titre in titres_bruts if isinstance(titre, str)}
                        trouvees_bloc = [ligne for ligne in lignes_bloc if ligne[0] in titres_retenus]
                else:
                    trouvees_bloc = extraire_lignes_bloc_qwen(resultat["response"], lignes_bloc)
            except (KeyError, ValueError, TypeError):
                erreur_bloc = "reponse_invalide"

        if args.max_resultats is not None:
            restant = max(args.max_resultats - len(toutes_correspondances), 0)
            trouvees_bloc = trouvees_bloc[:restant]

        toutes_correspondances.extend(trouvees_bloc)

        debut = (numero - 1) * taille_bloc + 1
        fin = debut + len(lignes_bloc) - 1
        duree_s = round(resultat.get("total_duration", 0) / 1e9, 2)
        statut_txt = f" ({erreur_bloc})" if erreur_bloc else ""
        print(
            f"[Bloc {numero}/{len(blocs)} · lignes {debut}-{fin}] "
            f"{len(trouvees_bloc)} trouvée(s) en {duree_s}s{statut_txt}",
            file=sys.stderr,
        )

        if not args.json and trouvees_bloc:
            writer_direct.writerows(trouvees_bloc)
            sys.stdout.flush()

        entree_bloc: dict[str, Any] = {
            "bloc": numero,
            "lignes": f"{debut}-{fin}",
            "prompt_eval_count": resultat.get("prompt_eval_count"),
            "eval_count": resultat.get("eval_count"),
            "done_reason": resultat.get("done_reason"),
            "secondes": duree_s,
            "correspondances": [
                dict(zip(("libelle", "facture", "page", "article"), ligne)) for ligne in trouvees_bloc
            ],
        }
        if erreur_bloc:
            entree_bloc["erreur"] = erreur_bloc
        rapport_blocs.append(entree_bloc)

        if args.max_resultats is not None and len(toutes_correspondances) >= args.max_resultats:
            if not est_dernier:
                decharger_modele(args.modele)
            break

    if args.mesures:
        print(json.dumps(totaux), file=sys.stderr)

    if args.json:
        sortie = {
            "modele": args.modele,
            "terme": terme,
            "contexte": args.contexte,
            "taille_bloc": taille_bloc,
            "blocs": rapport_blocs,
            "correspondances": [
                dict(zip(("libelle", "facture", "page", "article"), ligne)) for ligne in toutes_correspondances
            ],
        }
        print(json.dumps(sortie, ensure_ascii=False, indent=2))
    elif not toutes_correspondances:
        print("Aucune correspondance trouvée.")


def executer_appel_unitaire(args: argparse.Namespace, tableau: str, terme: str) -> None:
    """Envoie l'intégralité du tableau CSV en un seul appel à Ollama."""
    mistral = args.modele in ("mistral:7b", "mistral") and not args.consigne_commune
    llama = args.modele == "llama3.2:3b" and not args.consigne_commune

    donnees: dict[str, Any] = {
        "model": args.modele,
        "stream": False,
        "keep_alive": 0,
        "options": {
            "temperature": 0,
            "seed": 42,
            "num_predict": 200 if (mistral or llama) else 300,
        },
    }
    if args.contexte is not None:
        donnees["options"]["num_ctx"] = args.contexte

    lignes_llama: dict[str, list[str]] = {}
    if mistral:
        endpoint = "chat"
        donnees.update(preparer_mistral(tableau, terme))
    elif llama:
        endpoint = "chat"
        demande, lignes_llama = preparer_llama(tableau, terme)
        donnees.update(demande)
    else:
        endpoint = "generate"
        donnees.update(
            {
                "system": SYSTEME_QWEN_RECHERCHE,
                "prompt": construire_prompt_qwen(tableau, terme),
            }
        )

    resultat = appeler_ollama(endpoint, donnees, timeout=180)
    if args.mesures:
        print(json.dumps(extraire_mesures(resultat)), file=sys.stderr)

    if llama:
        afficher_llama(resultat["message"]["content"], lignes_llama, args.max_resultats)
    elif mistral:
        contenu = resultat["message"]["content"]
        if args.max_resultats is not None:
            donnees_mistral = json.loads(contenu)
            donnees_mistral["lignes"] = donnees_mistral["lignes"][: args.max_resultats]
            contenu = json.dumps(donnees_mistral, ensure_ascii=False, separators=(",", ":"))
        print(contenu)
    else:
        contenu = resultat["response"]
        if args.max_resultats is not None:
            contenu = limiter_resultats_texte(contenu, tableau, args.max_resultats)
        print(contenu)


def executer_comparatif_format(args: argparse.Namespace, tableau: str, terme: str) -> None:
    """Compare l'envoi de l'inventaire en CSV texte vs JSON structuré avec vérification locale PASS/FAIL."""
    dossier_prompts = Path(__file__).resolve().parent.parent / "prompts"
    if args.prompt is not None:
        source_prompt = args.prompt
    else:
        suffixe = "eng" if args.langue == "eng" else "fr"
        source_prompt = dossier_prompts / f"recherche-inventaire-{suffixe}.txt"
    if not source_prompt.is_file():
        raise SystemExit(f"Fichier prompt introuvable : {source_prompt}")

    systeme = source_prompt.read_text(encoding="utf-8").strip()
    est_eng = args.langue == "eng" or source_prompt.stem.endswith("-eng")

    lecteur = list(csv.reader(StringIO(tableau), delimiter=";"))
    entete, lignes_donnees = lecteur[0], lecteur[1:]
    titres_reels = list(dict.fromkeys(ligne[0] for ligne in lignes_donnees))
    attendus = [t for t in titres_reels if terme in t.lower()]

    if args.format_entree == "json":
        objets = [dict(zip(entete, ligne)) for ligne in lignes_donnees]
        bloc_donnees = json.dumps(objets, ensure_ascii=False, separators=(",", ":"))
        etiquette = "JSON"
    else:
        bloc_donnees = tableau.strip()
        etiquette = "CSV"

    contenu_user = (
        f"Search substring: `{terme}`\nInventory ({etiquette}):\n{bloc_donnees}\nReturn JSON `{{\"titres\": [...]}}`."
        if est_eng
        else f"Sous-chaîne recherchée : `{terme}`\nInventaire ({etiquette}) :\n{bloc_donnees}\nRenvoie le JSON `{{\"titres\": [...]}}`."
    )

    options: dict[str, Any] = {
        "temperature": 0,
        "seed": 42,
        "num_ctx": args.contexte or 4096,
        "num_predict": 400,
    }
    if args.modele == "llama3.2:3b":
        options["repeat_penalty"] = 1.15
    elif args.modele.startswith("qwen2.5"):
        options["repeat_penalty"] = 1.05
        options["top_p"] = 0.8
        options["top_k"] = 20

    schema = {
        "type": "object",
        "required": ["titres"],
        "additionalProperties": False,
        "properties": {
            "titres": {
                "type": "array",
                "items": {"type": "string", "enum": titres_reels},
            }
        },
    }
    donnees = {
        "model": args.modele,
        "stream": False,
        "keep_alive": 0,
        "options": options,
        "format": schema,
        "messages": [
            {"role": "system", "content": systeme},
            {"role": "user", "content": contenu_user},
        ],
    }

    resultat = appeler_ollama("chat", donnees, timeout=180)
    mesures = extraire_mesures(resultat)
    duree_s = round(resultat.get("total_duration", 0) / 1e9, 2)
    if args.mesures:
        print(json.dumps(mesures), file=sys.stderr)

    try:
        obtenus = list(dict.fromkeys(json.loads(resultat["message"]["content"]).get("titres", [])))
    except ValueError:
        obtenus = []

    vrais_positifs = [t for t in obtenus if t in attendus]
    faux_positifs = [t for t in obtenus if t not in attendus]
    manques = [t for t in attendus if t not in obtenus]
    statut = "PASS" if (not manques and not faux_positifs and mesures.get("done_reason") == "stop") else "FAIL"

    print(
        f"=== Recherche `{terme}` · {args.modele} · format={etiquette} "
        f"({duree_s}s · {mesures.get('prompt_eval_count')} tok in -> {mesures.get('eval_count')} tok out · {mesures.get('done_reason')}) ==="
    )
    print(f"Prompt : {source_prompt.name}")
    print(f"Résultats ({len(vrais_positifs)}/{len(attendus)}, {len(faux_positifs)} faux positif) : {obtenus}")
    if manques:
        print(f"Manqués : {manques}")
    if faux_positifs:
        print(f"Faux positifs : {faux_positifs}")
    print(f"Vérification locale : {statut}")


def construire_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments en ligne de commande."""
    parser = argparse.ArgumentParser(
        description="Cherche un titre dans l’inventaire avec un modèle local Ollama."
    )
    parser.add_argument(
        "--modele",
        default="qwen2.5:3b",
        help="Nom du modèle Ollama (par défaut : qwen2.5:3b)",
    )
    parser.add_argument(
        "--consigne-commune",
        action="store_true",
        help="Rejoue le prompt identique sur tous les modèles",
    )
    parser.add_argument(
        "--contexte",
        type=int,
        metavar="TOKENS",
        help="Taille du contexte Ollama pour cet appel, en tokens",
    )
    parser.add_argument(
        "--mesures",
        action="store_true",
        help="Affiche les compteurs renvoyés par Ollama",
    )
    parser.add_argument(
        "--lignes-fictives",
        type=int,
        nargs="?",
        const=3000,
        default=0,
        help="Ajoute des lignes de test au tableau envoyé à Ollama (3 000 si aucun nombre n'est fourni)",
    )
    parser.add_argument(
        "--max-lignes",
        type=int,
        metavar="LIGNES",
        help="Limite l'analyse aux N premières lignes de données du CSV",
    )
    parser.add_argument(
        "--max-resultats",
        type=int,
        metavar="RESULTATS",
        help="Limite le nombre de lignes correspondantes renvoyées en sortie",
    )
    parser.add_argument(
        "--taille-bloc",
        type=int,
        metavar="LIGNES",
        help="Découpe le CSV en blocs de N lignes traités séparément",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Exécute par blocs et renvoie le bilan complet en JSON",
    )
    parser.add_argument(
        "--format-entree",
        choices=("csv", "json"),
        help="Active le mode comparatif d'entrée (`csv` texte vs `json` structuré) avec vérification locale PASS/FAIL",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        metavar="FICHIER",
        help="Fichier prompt externe pour `--format-entree` (par défaut : prompts/recherche-inventaire-{fr|eng}.txt)",
    )
    parser.add_argument(
        "--langue",
        choices=("fr", "eng"),
        default="eng",
        help="Langue du prompt externe pour `--format-entree` (`fr` ou `eng`, par défaut : `eng`)",
    )
    return parser


def valider_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Vérifie la cohérence des bornes numériques passées en ligne de commande."""
    if args.contexte is not None and args.contexte <= 0:
        parser.error("--contexte attend un nombre de tokens supérieur à zéro.")
    if args.lignes_fictives < 0:
        parser.error("--lignes-fictives attend un nombre positif ou nul.")
    if args.max_lignes is not None and args.max_lignes <= 0:
        parser.error("--max-lignes attend un nombre de lignes supérieur à zéro.")
    if args.max_resultats is not None and args.max_resultats <= 0:
        parser.error("--max-resultats attend un nombre de résultats supérieur à zéro.")
    if args.taille_bloc is not None and args.taille_bloc <= 0:
        parser.error("--taille-bloc attend un nombre de lignes supérieur à zéro.")


def main() -> None:
    """Point d'entrée du script de recherche dans l'inventaire."""
    parser = construire_parser()
    args = parser.parse_args()
    valider_arguments(parser, args)

    # `lstrip("﻿")` : le pipe PowerShell 5.1 peut préfixer la saisie d'un BOM UTF-8.
    titre = input("Quel titre cherches-tu ? ").lstrip("﻿").strip()
    if not titre:
        raise SystemExit("Saisis un titre ou un mot à chercher.")

    try:
        tableau = lire_tableau()
    except (OSError, KeyError, ValueError) as erreur:
        raise SystemExit(f"Lecture du classeur impossible ({CLASSEUR_DEFAUT}) : {erreur}") from None

    if args.lignes_fictives:
        tableau = ajouter_lignes_fictives(tableau, args.lignes_fictives)
        print(f"{args.lignes_fictives} lignes fictives ajoutées pour cet essai.", file=sys.stderr)

    if args.max_lignes is not None:
        nb_disponibles = max(len(tableau.splitlines()) - 1, 0)
        tableau = limiter_lignes(tableau, args.max_lignes)
        print(
            f"Analyse limitée aux {min(args.max_lignes, nb_disponibles)} premières lignes du CSV.",
            file=sys.stderr,
        )

    terme = titre.lower()
    try:
        if args.format_entree is not None:
            executer_comparatif_format(args, tableau, terme)
        elif args.taille_bloc is not None or args.json:
            executer_par_blocs(args, tableau, terme)
        else:
            executer_appel_unitaire(args, tableau, terme)
    except HTTPError as erreur:
        raise SystemExit(f"Ollama a renvoyé une erreur HTTP {erreur.code}.") from None
    except (URLError, TimeoutError) as erreur:
        raise SystemExit(f"Ollama est injoignable : {erreur}") from None
    except (KeyError, ValueError) as erreur:
        raise SystemExit(f"Réponse Ollama inexploitable : {erreur}") from None


if __name__ == "__main__":
    main()
