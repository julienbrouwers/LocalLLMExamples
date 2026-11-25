"""Évalue l'analyse de code PHP fonction par fonction avec un modèle local Ollama.

Compare deux approches sur les 5 fonctions de `calculs_legacy.php` envoyées en direct
(une fonction isolée à la fois, sans historique cumulé) :
- Prompt V1 (historisé, `--ancien-prompt`) : demande une explication libre avec un exemple chiffré
  calculé de tête par le LLM (source d'erreurs arithmétiques sur les taux, boucles et puissances).
- Prompt V2 amélioré (par défaut) : sépare strictement les rôles : le LLM extrait la règle métier,
  repère les cas limites du code et génère des cas de test structurés en JSON, puis le programme
  exécute les vrais calculs déterministes (sans laisser le LLM faire de calcul mental).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from commun import RACINE, appeler_ollama, decharger_modele, extraire_mesures


FICHIER_PHP_DEFAUT: Path = RACINE / "calculs_legacy.php"
DOSSIER_PROMPTS: Path = Path(__file__).resolve().parent.parent / "prompts"


# --- Historique des prompts ---
# Prompt V1 (historisé) : demande au LLM d'expliquer les règles ET de dérouler lui-même un calcul chiffré.
# Limite observée : même avec une seule fonction isolée, les modèles 3B/7B font des erreurs de calcul
# mental (inversion HT/TTC, oubli des 120 mois d'épargne, approximation des intérêts composés).
SYSTEME_PHP_V1_HISTORIQUE: str = (
    "Tu analyses une fonction PHP fournie en direct dans le prompt. "
    "Réponds en français uniquement. Explique :\n"
    "1. Les entrées et la valeur renvoyée.\n"
    "2. Les règles métier dans l'ordre d'exécution avec les lignes qui les justifient.\n"
    "3. Un exemple chiffré complet calculé étape par étape.\n"
    "4. Au moins un cas limite."
)

# Prompt V2 (historisé, `--version-prompt v2`) : premier palier structuré JSON (règles + cas limites + cas de test).
# Limites observées sur `calculTva` : Qwen affirmait à tort que codeTaux inconnu renvoie des zéros partout,
# Llama hallucinait une division par zéro sur `codeTaux == 2`, et Mistral omettait les taux exacts.
SYSTEME_PHP_V2_AMELIORE: str = (
    "Tu es un développeur PHP senior. On te fournit le code d'une seule fonction PHP en direct "
    "(tu n'as pas accès aux fichiers du disque). Réponds uniquement en français et en JSON valide.\n"
    "Règles strictes :\n"
    "1. Résume le rôle de la fonction en une phrase précise.\n"
    "2. Liste au maximum 6 règles métier exactes : pour chaque `if` ou opérateur ternaire `(cond) ? A : B`, "
    "associe explicitement `si cond -> résultat A, sinon -> résultat B` sans inverser les branches.\n"
    "3. Signale au maximum 4 cas limites ou bugs potentiels (division par zéro, code inconnu, valeurs négatives ou nulles).\n"
    "4. Propose exactement 3 jeux de test (nominal, seuil exact, cas limite) avec uniquement leurs paramètres "
    "d'entrée en JSON : ne calcule PAS les résultats de sortie toi-même."
)

# Prompt V3 généraliste commun (utilisé par Mistral 7B et avec `--consigne-commune`) :
# 100 % agnostique de la fonction analysée (~450-550 tokens en entrée) pour fonctionner sur n'importe
# quelle fonction PHP tout en restant rapide sur Mistral 7B (préfill à 49 % CPU sur 4 Go VRAM).
SYSTEME_PHP_V3_EXPERT: str = (
    "Tu es un auditeur PHP senior. On te fournit UNE fonction PHP en direct. "
    "Réponds en français uniquement et en JSON strict conforme au schéma :\n"
    "1. `resume` : 1 phrase exacte décrivant l'objectif métier de la fonction.\n"
    "2. `parametres` : chaque paramètre de la signature (`nom`, `defaut` = 'obligatoire' s'il n'a pas de `=...`, et `role` précis).\n"
    "3. `branches` : CHAQUE condition (`if`, `elseif`, `else`, opérateur ternaire `? A : B`) dans l'ordre du code, "
    "avec `condition` exacte et `effet` exact sans jamais inverser les branches.\n"
    "4. `retour` : type renvoyé, noms exacts des clés du tableau de sortie (le cas échéant) et arrondis appliqués.\n"
    "5. `cas_limites` : 2 à 3 remarques factuelles tirées uniquement du code (valeurs nulles/négatives, branche par défaut `else`, "
    "division par zéro réelle uniquement si un dénominateur peut valoir 0).\n"
    "6. `cas_test` : exactement 4 jeux d'entrée distincts couvrant le cas nominal, une branche alternative, "
    "un seuil ou une valeur par défaut, et un cas limite (zéro ou valeur hors grille). Ne calcule PAS les sorties."
)

# Adaptation V3 généraliste dédiée Qwen 2.5 3B (ChatML + doc Qwen : repeat_penalty=1.05, top_p=0.8, top_k=20)
# Renforce la lecture stricte des opérateurs ternaires `? A : B` et la cohérence entre `branches` et `cas_limites`.
SYSTEME_PHP_V3_QWEN: str = (
    SYSTEME_PHP_V3_EXPERT
    + "\nRègles de rigueur :\n"
    "- Pour tout opérateur ternaire `(cond) ? A : B`, vérifie que `cond` vraie donne `A` et `cond` fausse donne `B` sans inverser.\n"
    "- `cas_limites` ne doit jamais contredire les formules listées dans `branches` et ne doit signaler une division par zéro "
    "que si un dénominateur du code peut réellement valoir 0."
)

# Adaptation V3 généraliste dédiée Llama 3.2 3B (Meta headers + instructions courtes + schéma `retour` structuré)
# Empêche `defaut: ""`, l'omission du `else` dans `branches`, et `retour: "array"` trop laconique.
SYSTEME_PHP_V3_LLAMA: str = (
    "Tu es un auditeur PHP senior. Analyse la fonction PHP fournie et réponds uniquement en français et en JSON strict.\n"
    "- `resume` : 1 phrase précise sur le rôle métier de la fonction.\n"
    "- `parametres` : liste tous les paramètres de la signature dans l'ordre. Si un paramètre n'a pas de `=...`, "
    "écris EXACTEMENT `obligatoire` dans `defaut` (jamais une chaîne vide).\n"
    "- `branches` : 1 seule entrée par branche logique (`if`, `elseif`, `else`, ternaire `? A : B`) dans l'ordre du code, "
    "en regroupant les calculs d'un même bloc dans une seule entrée et en incluant toujours le `else` final.\n"
    "- `retour` : renseigne `type`, la liste exacte des `cles` du tableau renvoyé (liste vide si scalaire/booléen) "
    "et la règle d'`arrondi` (`aucun` s'il n'y a pas de `round`).\n"
    "- `cas_limites` : 2 à 3 constats factuels tirés uniquement du code (valeur hors grille/défaut, zéro ou négatif, "
    "division par zéro uniquement si un dénominateur peut valoir 0).\n"
    "- `cas_test` : exactement 4 jeux d'entrée distincts (nominal, branche alternative, seuil/valeur par défaut, cas limite). "
    "Ne calcule PAS les sorties."
)


# --- Vérité terrain : équivalent déterministe des 5 fonctions PHP (arrondi half-up à 2 décimales) ---
def _arrondi(valeur: float | int, decimales: int = 2) -> float:
    """Reproduit l'arrondi standard PHP `round($val, $decimales)` (half-up)."""
    quant = Decimal("1") if decimales == 0 else Decimal(f"1e-{decimales}")
    return float(Decimal(str(valeur)).quantize(quant, rounding=ROUND_HALF_UP))


def php_calcul_tva(montant: float, codeTaux: int = 1, depuisTtc: bool = False) -> dict[str, float]:
    """Équivalent déterministe de `calculTva()`."""
    grille = {1: 0.20, 2: 0.10, 3: 0.055, 4: 0.021}
    taux = grille.get(codeTaux, 0.0)
    if not depuisTtc:
        ttc = montant * (1 + taux)
        tva = ttc - montant
        ht = montant
    else:
        ht = montant / (1 + taux)
        tva = montant - ht
        ttc = montant
    return {"ht": _arrondi(ht), "tva": _arrondi(tva), "ttc": _arrondi(ttc)}


def php_stats_marge(achatHt: float, venteTtc: float, tva: float = 20) -> dict[str, float] | bool:
    """Équivalent déterministe de `statsMarge()`."""
    if achatHt <= 0 or venteTtc <= 0:
        return False
    vente_ht = venteTtc / (1 + (tva / 100))
    marge = vente_ht - achatHt
    return {
        "marge_ht": _arrondi(marge),
        "taux_marge": _arrondi((marge / achatHt) * 100),
        "taux_marque": _arrondi((marge / vente_ht) * 100),
        "coeff": _arrondi(venteTtc / achatHt, 3),
    }


def php_calculer_facture(lignes: list[dict[str, Any]], remiseGlobale: float = 0) -> dict[str, Any]:
    """Équivalent déterministe de `calculerFacture()`."""
    total_ht = 0.0
    tva_par_taux: dict[str, float] = {}
    grille_tva = {1: 0.20, 2: 0.10, 3: 0.055, 4: 0.021}
    for ligne in lignes:
        qte = ligne["qte"]
        pu = ligne["pu"]
        rem_ligne = ligne.get("remise", 0)
        code_tva = ligne.get("tva", 1)
        net_ligne = qte * pu * (1 - (rem_ligne / 100)) * (1 - (remiseGlobale / 100))
        total_ht += net_ligne
        tx = grille_tva.get(code_tva, 0.20)
        cle = str(tx * 100).rstrip("0").rstrip(".") if (tx * 100).is_integer() else str(tx * 100)
        tva_par_taux[cle] = tva_par_taux.get(cle, 0.0) + net_ligne * tx

    port = 0.0 if (total_ht >= 150 or total_ht == 0) else 9.90
    if port > 0:
        tva_par_taux["20"] = tva_par_taux.get("20", 0.0) + port * 0.20

    total_tva = 0.0
    for cle, val in tva_par_taux.items():
        tva_par_taux[cle] = _arrondi(val)
        total_tva += tva_par_taux[cle]

    return {
        "total_ht": _arrondi(total_ht + port),
        "frais_port": port,
        "detail_tva": tva_par_taux,
        "total_tva": _arrondi(total_tva),
        "total_ttc": _arrondi(total_ht + port + total_tva),
    }


def php_calcul_mensualite(capital: float, tauxAnnuel: float, annees: int) -> dict[str, float]:
    """Équivalent déterministe de `calculMensualite()`."""
    nb_mois = annees * 12
    taux_mensuel = (tauxAnnuel / 100) / 12
    if taux_mensuel == 0:
        mensualite = capital / nb_mois
    else:
        mensualite = capital * (taux_mensuel / (1 - (1 + taux_mensuel) ** (-nb_mois)))
    total_rembourse = mensualite * nb_mois
    return {
        "mensualite": _arrondi(mensualite),
        "total": _arrondi(total_rembourse),
        "interets": _arrondi(total_rembourse - capital),
    }


def php_simulation_epargne(
    capitalInit: float,
    versementMois: float,
    tauxAnnuel: float,
    annees: int,
    avecFlatTax: bool = True,
) -> dict[str, float]:
    """Équivalent déterministe de `simulationEpargne()`."""
    nb_mois = annees * 12
    taux_mensuel = (tauxAnnuel / 100) / 12
    solde = float(capitalInit)
    for _ in range(nb_mois):
        solde = (solde + versementMois) * (1 + taux_mensuel)
    total_verse = capitalInit + (versementMois * nb_mois)
    plus_value = solde - total_verse
    impot = plus_value * 0.30 if (avecFlatTax and plus_value > 0) else 0.0
    return {
        "verse": _arrondi(total_verse),
        "brut": _arrondi(solde),
        "plus_value": _arrondi(plus_value),
        "impot": _arrondi(impot),
        "net": _arrondi(solde - impot),
    }


IMPLEMENTATIONS_PHP: dict[str, tuple[Any, str, dict[str, Any]]] = {
    "calculTva": (
        php_calcul_tva,
        "montant (float), codeTaux (1|2|3|4|autre), depuisTtc (bool)",
        {
            "type": "object",
            "required": ["montant", "codeTaux", "depuisTtc"],
            "additionalProperties": False,
            "properties": {
                "montant": {"type": "number"},
                "codeTaux": {"type": "integer"},
                "depuisTtc": {"type": "boolean"},
            },
        },
    ),
    "statsMarge": (
        php_stats_marge,
        "achatHt (float), venteTtc (float), tva (float, pourcentage ex: 20)",
        {
            "type": "object",
            "required": ["achatHt", "venteTtc", "tva"],
            "additionalProperties": False,
            "properties": {
                "achatHt": {"type": "number"},
                "venteTtc": {"type": "number"},
                "tva": {"type": "number"},
            },
        },
    ),
    "calculerFacture": (
        php_calculer_facture,
        "lignes (liste d'objets {qte, pu, remise, tva}), remiseGlobale (float)",
        {
            "type": "object",
            "required": ["lignes", "remiseGlobale"],
            "additionalProperties": False,
            "properties": {
                "lignes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["qte", "pu", "remise", "tva"],
                        "additionalProperties": False,
                        "properties": {
                            "qte": {"type": "number"},
                            "pu": {"type": "number"},
                            "remise": {"type": "number"},
                            "tva": {"type": "integer"},
                        },
                    },
                },
                "remiseGlobale": {"type": "number"},
            },
        },
    ),
    "calculMensualite": (
        php_calcul_mensualite,
        "capital (float), tauxAnnuel (float), annees (int)",
        {
            "type": "object",
            "required": ["capital", "tauxAnnuel", "annees"],
            "additionalProperties": False,
            "properties": {
                "capital": {"type": "number"},
                "tauxAnnuel": {"type": "number"},
                "annees": {"type": "integer"},
            },
        },
    ),
    "simulationEpargne": (
        php_simulation_epargne,
        "capitalInit (float), versementMois (float), tauxAnnuel (float), annees (int), avecFlatTax (bool)",
        {
            "type": "object",
            "required": ["capitalInit", "versementMois", "tauxAnnuel", "annees", "avecFlatTax"],
            "additionalProperties": False,
            "properties": {
                "capitalInit": {"type": "number"},
                "versementMois": {"type": "number"},
                "tauxAnnuel": {"type": "number"},
                "annees": {"type": "integer"},
                "avecFlatTax": {"type": "boolean"},
            },
        },
    ),
}


def extraire_fonctions_php(chemin_php: Path) -> list[tuple[str, str]]:
    """Découpe un fichier PHP en couples `(nom_fonction, code_source)` sans charger tout le fichier."""
    lignes = chemin_php.read_text(encoding="utf-8").splitlines()
    fonctions: list[tuple[str, str]] = []
    bloc_courant: list[str] = []
    nom_courant: str | None = None
    profondeur = 0

    for ligne in lignes:
        if nom_courant is None:
            correspondance = re.match(r"^\s*function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", ligne)
            if correspondance:
                nom_courant = correspondance.group(1)
                bloc_courant = [ligne]
                profondeur = ligne.count("{") - ligne.count("}")
        else:
            bloc_courant.append(ligne)
            profondeur += ligne.count("{") - ligne.count("}")
            if profondeur <= 0 and "}" in ligne:
                fonctions.append((nom_courant, "\n".join(bloc_courant)))
                nom_courant = None
                bloc_courant = []
                profondeur = 0

    return fonctions


def preparer_requete_v2(nom: str, code: str) -> dict[str, Any]:
    """Construit la requête `/api/chat` structurée (règles + cas limites + 3 jeux d'entrée JSON)."""
    _, description_entrees, schema_entrees = IMPLEMENTATIONS_PHP[nom]
    schema = {
        "type": "object",
        "required": ["role", "regles", "cas_limites", "cas_test"],
        "additionalProperties": False,
        "properties": {
            "role": {"type": "string"},
            "regles": {"type": "array", "items": {"type": "string"}},
            "cas_limites": {"type": "array", "items": {"type": "string"}},
            "cas_test": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["nom", "entrees"],
                    "additionalProperties": False,
                    "properties": {
                        "nom": {"type": "string"},
                        "entrees": schema_entrees,
                    },
                },
            },
        },
    }
    messages = [
        {"role": "system", "content": SYSTEME_PHP_V2_AMELIORE},
        {
            "role": "user",
            "content": (
                f"Fonction PHP à analyser : `{nom}`\n"
                f"Paramètres attendus pour chaque jeu de test (`entrees`) : {description_entrees}.\n\n"
                f"Code PHP envoyé en direct :\n```php\n{code}\n```"
            ),
        },
    ]
    return {"messages": messages, "format": schema}


def options_par_modele(modele: str, contexte: int, version_prompt: str, consigne_commune: bool = False) -> dict[str, Any]:
    """Construit les options d'échantillonnage Ollama alignées sur les docs officielles de chaque LLM."""
    options: dict[str, Any] = {
        "temperature": 0,
        "seed": 42,
        "num_ctx": contexte,
        "num_predict": 1400 if version_prompt == "v3" else 900,
    }
    if modele == "llama3.2:3b":
        # Empêche les boucles JSON/répétitions sur Llama 3.2 3B
        options["repeat_penalty"] = 1.15
    elif modele.startswith("qwen2.5") and not consigne_commune:
        # Doc officielle HuggingFace Qwen2.5-3B-Instruct : repeat_penalty modeste (1.05), top_p=0.8, top_k=20
        options["repeat_penalty"] = 1.05
        options["top_p"] = 0.8
        options["top_k"] = 20
    # Mistral 7B : volontairement SANS repeat_penalty (1.0 par défaut) pour ne pas bloquer les tokens métier répétés
    return options


def charger_prompt_systeme(
    modele: str,
    consigne_commune: bool = False,
    fichier_prompt: Path | None = None,
    langue: str = "fr",
) -> tuple[str, Path | None]:
    """Charge le prompt système V3 depuis un fichier externe (`prompts/php-*-{fr|eng}.txt`)."""
    if fichier_prompt is not None:
        return fichier_prompt.read_text(encoding="utf-8").strip(), fichier_prompt

    suffixe = "eng" if langue == "eng" else "fr"
    if not consigne_commune and modele == "llama3.2:3b":
        candidat, repli = DOSSIER_PROMPTS / f"php-llama-{suffixe}.txt", SYSTEME_PHP_V3_LLAMA
    elif not consigne_commune and modele.startswith("qwen2.5"):
        candidat, repli = DOSSIER_PROMPTS / f"php-qwen-{suffixe}.txt", SYSTEME_PHP_V3_QWEN
    else:
        candidat, repli = DOSSIER_PROMPTS / f"php-mistral-{suffixe}.txt", SYSTEME_PHP_V3_EXPERT

    if candidat.is_file():
        return candidat.read_text(encoding="utf-8").strip(), candidat
    return repli, None


def compter_branches_php(code: str) -> int:
    """Compte dynamiquement les clauses conditionnelles (`if`/`elseif`/`else` et ternaires) du code PHP."""
    nb_if_else = len(re.findall(r"\b(?:if|elseif|else)\b", code))
    nb_ternaires = len(re.findall(r"\?[^?:]+:", code))
    return nb_if_else + nb_ternaires


def preparer_requete_v3(
    nom: str,
    code: str,
    modele: str = "qwen2.5:3b",
    consigne_commune: bool = False,
    fichier_prompt: Path | None = None,
    langue: str = "fr",
) -> tuple[dict[str, Any], Path | None]:
    """Construit la requête `/api/chat` V3 avec prompt externe chargé depuis un fichier."""
    _, description_entrees, schema_entrees = IMPLEMENTATIONS_PHP[nom]
    est_eng = langue == "eng" or (fichier_prompt is not None and fichier_prompt.stem.endswith("-eng"))
    systeme, source_prompt = charger_prompt_systeme(modele, consigne_commune, fichier_prompt, "eng" if est_eng else "fr")
    nb_branches = compter_branches_php(code)

    schema_retour: dict[str, Any] = {
        "type": "object",
        "required": ["type", "cles", "arrondi"],
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string"},
            "cles": {"type": "array", "items": {"type": "string"}},
            "arrondi": {"type": "string"},
        },
    }

    schema_branches: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["condition", "effet"],
            "additionalProperties": False,
            "properties": {
                "condition": {"type": "string"},
                "effet": {"type": "string"},
            },
        },
    }
    if nb_branches > 0:
        schema_branches["minItems"] = nb_branches
        schema_branches["maxItems"] = nb_branches + 2

    schema = {
        "type": "object",
        "required": ["resume", "parametres", "branches", "retour", "cas_limites", "cas_test"],
        "additionalProperties": False,
        "properties": {
            "resume": {"type": "string"},
            "parametres": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["nom", "defaut", "role"],
                    "additionalProperties": False,
                    "properties": {
                        "nom": {"type": "string", "minLength": 1},
                        "defaut": {"type": "string", "minLength": 1},
                        "role": {"type": "string", "minLength": 3},
                    },
                },
            },
            "branches": schema_branches,
            "retour": schema_retour,
            "cas_limites": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "string"}},
            "cas_test": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "required": ["nom", "entrees"],
                    "additionalProperties": False,
                    "properties": {
                        "nom": {"type": "string"},
                        "entrees": schema_entrees,
                    },
                },
            },
        },
    }
    contenu_user = (
        (
            f"PHP function to analyze: `{nom}`\n"
            f"Expected parameters for each test case (`entrees`): {description_entrees}.\n\n"
            f"Inline PHP code:\n```php\n{code}\n```"
        )
        if est_eng
        else (
            f"Fonction PHP à analyser : `{nom}`\n"
            f"Paramètres attendus pour chaque jeu de test (`entrees`) : {description_entrees}.\n\n"
            f"Code PHP envoyé en direct :\n```php\n{code}\n```"
        )
    )
    messages = [
        {"role": "system", "content": systeme},
        {"role": "user", "content": contenu_user},
    ]
    return {"messages": messages, "format": schema}, source_prompt


def executer_cas_test(nom: str, cas_test: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exécute de façon déterministe les cas de test proposés par le LLM."""
    fn, _, _ = IMPLEMENTATIONS_PHP[nom]
    rapports: list[dict[str, Any]] = []
    for cas in cas_test:
        entrees = cas.get("entrees", {})
        try:
            sortie = fn(**entrees)
            rapports.append({"nom": cas.get("nom", ""), "entrees": entrees, "resultat_exact": sortie})
        except (ArithmeticError, KeyError, TypeError, ValueError) as erreur:
            rapports.append({"nom": cas.get("nom", ""), "entrees": entrees, "erreur_execution": f"{type(erreur).__name__}: {erreur}"})
    return rapports


def extraire_signature_php(code: str) -> list[tuple[str, str | None]]:
    """Extrait les paramètres réels `(nom_sans_dollar, defaut_ou_None)` de la signature PHP."""
    correspondance = re.search(r"function\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\(([^)]*)\)", code, re.S)
    if not correspondance:
        return []
    parametres: list[tuple[str, str | None]] = []
    for morceau in correspondance.group(1).split(","):
        morceau = morceau.strip()
        if not morceau:
            continue
        m_param = re.search(r"\$([a-zA-Z_][a-zA-Z0-9_]*)(?:\s*=\s*(.+))?$", morceau)
        if m_param:
            nom_p = m_param.group(1)
            defaut_p = m_param.group(2).strip() if m_param.group(2) is not None else None
            parametres.append((nom_p, defaut_p))
    return parametres


def verifier_analyse_locale(
    code: str,
    analyse: dict[str, Any],
    mesures: dict[str, Any],
    cas_executes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Vérifie objectivement en local la qualité de l'analyse PHP et des tests générés (100 % généraliste)."""
    controles: dict[str, dict[str, Any]] = {}

    # 1. JSON valide + arrêt propre
    json_ok = "erreur_json" not in analyse and mesures.get("done_reason") == "stop"
    controles["json_et_stop"] = {
        "ok": json_ok,
        "detail": f"done_reason={mesures.get('done_reason')}",
    }

    # 2. Paramètres complets + défauts non vides
    params_reels = extraire_signature_php(code)
    params_llm = {
        str(p.get("nom", "")).lstrip("$").strip(): str(p.get("defaut", "")).strip()
        for p in analyse.get("parametres", [])
        if isinstance(p, dict)
    }
    manquants = [nom for nom, _ in params_reels if nom not in params_llm]
    defauts_vides = [nom for nom, _ in params_reels if nom in params_llm and not params_llm[nom]]
    params_ok = not manquants and not defauts_vides
    controles["parametres"] = {
        "ok": params_ok,
        "detail": f"{len(params_reels) - len(manquants)}/{len(params_reels)} présents"
        + (f", manquants={manquants}" if manquants else "")
        + (f", defaut_vide={defauts_vides}" if defauts_vides else ""),
    }

    # 3. Branches conditionnelles présentes
    nb_if_else = len(re.findall(r"\b(?:if|elseif|else)\b", code))
    nb_ternaires = len(re.findall(r"\?[^?:]+:", code))
    min_branches = max(1, nb_if_else + nb_ternaires) if (nb_if_else + nb_ternaires) > 0 else 0
    branches_llm = analyse.get("branches", []) if isinstance(analyse.get("branches"), list) else []
    branches_ok = len(branches_llm) >= min_branches
    controles["branches"] = {
        "ok": branches_ok,
        "detail": f"{len(branches_llm)} décrite(s) (attendu >= {min_branches} d'après le code PHP)",
    }

    # 4. Valeur de retour : clés associatives + arrondi si `round(` présent
    cles_retour = re.findall(r"['\"]([a-zA-Z0-9_]+)['\"]\s*=>", code)
    retour_brut = analyse.get("retour", "")
    texte_retour = json.dumps(retour_brut, ensure_ascii=False).lower() if isinstance(retour_brut, dict) else str(retour_brut).lower()
    cles_manquantes = [cle for cle in dict.fromkeys(cles_retour) if cle.lower() not in texte_retour]
    a_round = "round(" in code
    arrondi_ok = True
    if a_round:
        if isinstance(retour_brut, dict):
            val_arr = str(retour_brut.get("arrondi", "")).lower()
            arrondi_ok = bool(val_arr) and not any(m in val_arr for m in ("aucun", "sans arrondi", "no rounding", "none"))
        else:
            arrondi_ok = any(mot in texte_retour for mot in ("round", "arrond", "décimal", "decimal", "2"))
    retour_ok = bool(texte_retour.strip()) and not cles_manquantes and arrondi_ok
    controles["retour"] = {
        "ok": retour_ok,
        "detail": (
            f"clés manquantes={cles_manquantes}" if cles_manquantes else f"{len(dict.fromkeys(cles_retour))} clé(s) présentes"
        )
        + (", arrondi manquant" if (a_round and not arrondi_ok) else ""),
    }

    # 5. Cas de test : 4 distincts exécutés sans erreur
    nb_exec = len(cas_executes)
    erreurs_exec = [c["nom"] for c in cas_executes if "erreur_execution" in c]
    entrees_uniques = {json.dumps(c.get("entrees", {}), sort_keys=True) for c in cas_executes}
    tests_ok = nb_exec == 4 and not erreurs_exec and len(entrees_uniques) == 4
    controles["tests"] = {
        "ok": tests_ok,
        "detail": f"{nb_exec}/4 exécuté(s), {len(entrees_uniques)} jeu(x) distinct(s)"
        + (f", erreurs={erreurs_exec}" if erreurs_exec else ""),
    }

    if all(c["ok"] for c in controles.values()):
        statut = "PASS"
    elif not json_ok or erreurs_exec:
        statut = "FAIL"
    else:
        statut = "PARTIAL"

    return {"statut": statut, "controles": controles}


def construire_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments en ligne de commande."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modele", default="qwen2.5:3b", help="Modèle Ollama (par défaut : qwen2.5:3b)")
    parser.add_argument("--fichier-php", "--fichier", dest="fichier_php", type=Path, default=FICHIER_PHP_DEFAUT, help="Fichier PHP à découper par fonction")
    parser.add_argument("--fonction", help="Nom d'une fonction précise à tester (sinon toutes les fonctions)")
    parser.add_argument(
        "--prompt",
        type=Path,
        metavar="FICHIER",
        help="Fichier texte contenant le prompt système généraliste (par défaut : prompts/php-*-{fr|eng}.txt)",
    )
    parser.add_argument(
        "--langue",
        choices=("fr", "eng"),
        default="fr",
        help="Langue du prompt externe par défaut (`fr` ou `eng`, par défaut : `fr`)",
    )
    parser.add_argument("--contexte", type=int, default=4096, metavar="TOKENS", help="Contexte Ollama (par défaut : 4096)")
    parser.add_argument(
        "--version-prompt",
        choices=("v1", "v2", "v3"),
        default="v3",
        help="Version du prompt à utiliser (v1 calcul mental, v2 JSON simple, v3 expert fidèle par défaut)",
    )
    parser.add_argument("--ancien-prompt", action="store_true", help="Alias pour `--version-prompt v1`")
    parser.add_argument(
        "--consigne-commune",
        action="store_true",
        help="Force le prompt V3 commun sur tous les modèles (désactive les adaptations par LLM)",
    )
    parser.add_argument("--sortie-json", type=Path, metavar="FICHIER", help="Enregistre le rapport complet en JSON")
    parser.add_argument("--mesures", action="store_true", help="Affiche les compteurs Ollama sur stderr")
    return parser


def main() -> None:
    """Point d'entrée du banc d'essai PHP fonction par fonction."""
    parser = construire_parser()
    args = parser.parse_args()
    version_prompt = "v1" if args.ancien_prompt else args.version_prompt

    if args.prompt is not None and not args.prompt.is_file():
        raise SystemExit(f"Fichier prompt introuvable : {args.prompt}")

    try:
        fonctions = extraire_fonctions_php(args.fichier_php)
    except OSError as erreur:
        raise SystemExit(f"Lecture du fichier PHP impossible ({args.fichier_php}) : {erreur}") from None

    if args.fonction:
        fonctions = [(n, c) for n, c in fonctions if n == args.fonction]
        if not fonctions:
            raise SystemExit(f"Fonction `{args.fonction}` introuvable dans {args.fichier_php}.")

    totaux = {"fonctions": len(fonctions), "prompt_eval_count": 0, "eval_count": 0, "total_duration": 0, "load_duration": 0}
    resultats: list[dict[str, Any]] = []

    for index, (nom, code) in enumerate(fonctions, 1):
        est_derniere = index == len(fonctions)
        options = options_par_modele(args.modele, args.contexte, version_prompt, args.consigne_commune)
        donnees: dict[str, Any] = {
            "model": args.modele,
            "stream": False,
            "keep_alive": 0 if est_derniere else "2m",
            "options": options,
        }
        source_prompt: Path | None = None
        if version_prompt == "v1":
            donnees["messages"] = [
                {"role": "system", "content": SYSTEME_PHP_V1_HISTORIQUE},
                {"role": "user", "content": f"Analyse la fonction `{nom}` :\n```php\n{code}\n```"},
            ]
        elif version_prompt == "v2":
            donnees.update(preparer_requete_v2(nom, code))
        else:
            requete_v3, source_prompt = preparer_requete_v3(nom, code, args.modele, args.consigne_commune, args.prompt, args.langue)
            donnees.update(requete_v3)

        try:
            reponse = appeler_ollama("chat", donnees, timeout=300)
        except HTTPError as erreur:
            decharger_modele(args.modele)
            raise SystemExit(f"Ollama a renvoyé HTTP {erreur.code} sur `{nom}`.") from None
        except (URLError, TimeoutError) as erreur:
            decharger_modele(args.modele)
            raise SystemExit(f"Ollama injoignable sur `{nom}` : {erreur}") from None

        for cle in ("prompt_eval_count", "eval_count", "total_duration", "load_duration"):
            totaux[cle] += reponse.get(cle, 0)

        contenu = reponse["message"]["content"]
        duree_s = round(reponse.get("total_duration", 0) / 1e9, 2)
        mesures_fn = extraire_mesures(reponse)

        if version_prompt == "v1":
            entree_resultat = {"fonction": nom, "mode": "V1_calcul_mental", "secondes": duree_s, "mesures": mesures_fn, "reponse_texte": contenu}
            print(f"\n=== [{index}/{len(fonctions)}] {nom} ({duree_s}s, V1) ===")
            print(contenu[:600] + ("..." if len(contenu) > 600 else ""))
        else:
            try:
                analyse = json.loads(contenu)
            except ValueError as erreur:
                analyse = {"erreur_json": str(erreur), "brut": contenu}
            cas_executes = executer_cas_test(nom, analyse.get("cas_test", [])) if nom in IMPLEMENTATIONS_PHP else []
            verification = verifier_analyse_locale(code, analyse, mesures_fn, cas_executes) if version_prompt == "v3" else None
            entree_resultat = {
                "fonction": nom,
                "mode": f"{version_prompt.upper()}_hybride_determine",
                "prompt_fichier": str(source_prompt) if source_prompt is not None else None,
                "secondes": duree_s,
                "mesures": mesures_fn,
                "analyse": analyse,
                "executions": cas_executes,
                "verification": verification,
            }
            print(f"\n=== [{index}/{len(fonctions)}] {nom} ({duree_s}s · {mesures_fn.get('prompt_eval_count')} tok in -> {mesures_fn.get('eval_count')} tok out · {mesures_fn.get('done_reason')}) ===")
            if source_prompt is not None:
                print(f"Prompt : {source_prompt}")
            if version_prompt == "v3":
                print(f"Résumé : {analyse.get('resume', '(absent)')}")
                print("Paramètres :")
                for p in analyse.get("parametres", []):
                    print(f"  - {p.get('nom')} (défaut : {p.get('defaut')}) : {p.get('role')}")
                print("Branches / conditions :")
                for b in analyse.get("branches", []):
                    print(f"  - {b.get('condition')} => {b.get('effet')}")
                print(f"Retour : {analyse.get('retour', '(absent)')}")
            else:
                print(f"Rôle : {analyse.get('role', '(absent)')}")
                print("Règles :")
                for regle in analyse.get("regles", []):
                    print(f"  - {regle}")
            print("Cas limites repérés :")
            for cl in analyse.get("cas_limites", []):
                print(f"  - {cl}")
            print("Cas de test proposés par le LLM -> exécutés de façon déterministe :")
            for ex in cas_executes:
                statut = ex.get("resultat_exact", ex.get("erreur_execution"))
                print(f"  * {ex['nom']} | entrées={ex['entrees']} => {statut}")
            if verification is not None:
                print(f"Vérification locale : {verification['statut']}")
                for nom_ctrl, res_ctrl in verification["controles"].items():
                    marque = "OK" if res_ctrl["ok"] else "KO"
                    print(f"  [{marque}] {nom_ctrl} : {res_ctrl['detail']}")

        resultats.append(entree_resultat)

    if args.mesures:
        print(json.dumps(totaux), file=sys.stderr)

    if args.sortie_json is not None:
        args.sortie_json.write_text(
            json.dumps({"modele": args.modele, "contexte": args.contexte, "totaux": totaux, "fonctions": resultats}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
