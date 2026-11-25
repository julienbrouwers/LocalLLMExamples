#requires -Version 5.1

<#
.SYNOPSIS
Compare des modèles Ollama avec la même consigne et les mêmes données.
.EXAMPLE
.\rejouer-essais-ollama.ps1 -Essai PHP -Langues FR,EN
.EXAMPLE
.\rejouer-essais-ollama.ps1 -Essai Facture -DonneesFacture .\donnees\entrees\champs-facture.txt -Langues FR,EN
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('PHP', 'Facture')]
    [string]$Essai,

    [ValidateSet('FR', 'EN')]
    [string[]]$Langues = @('FR'),

    [ValidateNotNullOrEmpty()]
    [string[]]$Modeles = @('llama3.2:3b', 'mistral:7b', 'qwen2.5:3b'),

    [string]$DonneesFacture,

    [string]$FichierPHP,

    [ValidateSet('Isoler', 'Separe', 'Partage')]
    [string]$ModeConversation = 'Isoler',

    [ValidateRange(1, 20)]
    [int]$Repetitions = 1,

    [ValidateRange(1024, 32768)]
    [int]$Contexte = 4096,

    [ValidateRange(32, 8192)]
    [int]$MaxTokens = 1024,

    [string]$DossierSortie = '.\resultats-ollama'
)

$ErrorActionPreference = 'Stop'
$adresseOllama = 'http://127.0.0.1:11434'
$utf8 = [Text.UTF8Encoding]::new($false)

function Invoke-Ollama {
    param(
        [string]$Chemin,
        [hashtable]$Corps
    )

    $uri = $adresseOllama + $Chemin
    if ($null -eq $Corps) {
        return Invoke-RestMethod -Uri $uri -TimeoutSec 600
    }

    $json = ConvertTo-Json -InputObject $Corps -Depth 8 -Compress
    $octets = [Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod -Uri $uri -Method Post -ContentType 'application/json; charset=utf-8' -Body $octets -TimeoutSec 600
}

function ConvertTo-Secondes {
    param($Nanosecondes)

    if ($null -ne $Nanosecondes) {
        [math]::Round([double]$Nanosecondes / 1e9, 3)
    }
}

function Get-FonctionsPHP {
    param([string]$CheminFichier)

    $fichierPhp = Get-Item -LiteralPath $CheminFichier
    $lignesPhp = [IO.File]::ReadAllLines($fichierPhp.FullName, $utf8)
    $fonctions = @()
    $nomCourant = $null
    $blocCourant = New-Object System.Collections.Generic.List[string]
    $profondeur = 0

    foreach ($ligneCourante in $lignesPhp) {
        if ($null -eq $nomCourant) {
            if ($ligneCourante -match '^\s*function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(') {
                $nomCourant = $Matches[1]
                $blocCourant.Clear()
                $blocCourant.Add($ligneCourante)
                $profondeur = ([regex]::Matches($ligneCourante, '\{').Count) - ([regex]::Matches($ligneCourante, '\}').Count)
            }
        } else {
            $blocCourant.Add($ligneCourante)
            $profondeur += ([regex]::Matches($ligneCourante, '\{').Count) - ([regex]::Matches($ligneCourante, '\}').Count)
            if ($profondeur -le 0 -and $ligneCourante.Contains('}')) {
                $fonctions += [pscustomobject]@{
                    Nom = $nomCourant
                    Code = ($blocCourant -join [string][char]10)
                }
                $nomCourant = $null
                $blocCourant.Clear()
                $profondeur = 0
            }
        }
    }
    return $fonctions
}

$fonctionsPhp = $null
if ($Essai -eq 'PHP') {
    if (-not [string]::IsNullOrWhiteSpace($FichierPHP)) {
        $fonctionsPhp = @(Get-FonctionsPHP -CheminFichier $FichierPHP)
        if ($fonctionsPhp.Count -eq 0) {
            throw "Aucune fonction PHP nommée trouvée dans $FichierPHP."
        }
        $consigneFR = @'
Analyse cette fonction PHP en français.

Explique :
1. Les entrées et les valeurs renvoyées.
2. Les règles dans leur ordre d'exécution et les lignes de code qui les justifient.
3. Un exemple chiffré et au moins un cas limite.

Voici la fonction :
'@
        $consigneEN = @'
Analyze this PHP function in English.

Explain:
1. The inputs and returned values.
2. The rules in their execution order and the lines of code that justify them.
3. A numerical example and at least one edge case.

Here is the function:
'@
    } else {
        $contenu = @'
function calculerFacture($lignes, $remiseGlobale = 0)
{
    $totalHt = 0;
    $tvaParTaux = [];
    $grilleTva = [1 => 0.20, 2 => 0.10, 3 => 0.055, 4 => 0.021];

    foreach ($lignes as $l) {
        $qte = $l['qte'];
        $pu = $l['pu'];
        $remLigne = isset($l['remise']) ? $l['remise'] : 0;
        $codeTva = isset($l['tva']) ? $l['tva'] : 1;

        $netLigne = $qte * $pu * (1 - ($remLigne / 100));
        $netLigne = $netLigne * (1 - ($remiseGlobale / 100));
        $totalHt += $netLigne;

        $tx = isset($grilleTva[$codeTva]) ? $grilleTva[$codeTva] : 0.20;
        $cle = (string) ($tx * 100);

        if (!isset($tvaParTaux[$cle])) {
            $tvaParTaux[$cle] = 0;
        }

        $tvaParTaux[$cle] += $netLigne * $tx;
    }

    $port = ($totalHt >= 150 || $totalHt == 0) ? 0 : 9.90;

    if ($port > 0) {
        if (!isset($tvaParTaux['20'])) $tvaParTaux['20'] = 0;
        $tvaParTaux['20'] += $port * 0.20;
    }

    $totalTva = 0;

    foreach ($tvaParTaux as $k => $v) {
        $tvaParTaux[$k] = round($v, 2);
        $totalTva += $tvaParTaux[$k];
    }

    return [
        'total_ht' => round($totalHt + $port, 2),
        'frais_port' => $port,
        'detail_tva' => $tvaParTaux,
        'total_tva' => round($totalTva, 2),
        'total_ttc' => round($totalHt + $port + $totalTva, 2)
    ];
}
'@
        $consigneFR = @'
Analyse cette fonction PHP en français.

Explique :
1. Ce que fait la fonction.
2. Comment sont calculés les remises, la TVA et les frais de port.
3. Les éventuels problèmes ou cas limites.

Voici la fonction :
'@
        $consigneEN = @'
Analyze this PHP function in English.

Explain:
1. What the function does.
2. How discounts, VAT, and shipping fees are calculated.
3. Any potential issues or edge cases.

Here is the function:
'@
    }
} else {
    if ([string]::IsNullOrWhiteSpace($DonneesFacture)) {
        throw "Pour l’essai Facture, indique -DonneesFacture avec le chemin du texte ou du JSON extrait."
    }

    $fichier = Get-Item -LiteralPath $DonneesFacture
    if ($fichier.PSIsContainer -or $fichier.Extension -eq '.pdf') {
        throw 'Fournis la sortie texte ou JSON du script Python.'
    }
    $contenu = [IO.File]::ReadAllText($fichier.FullName, $utf8)
    if ([string]::IsNullOrWhiteSpace($contenu)) {
        throw 'Le fichier de données de facture est vide.'
    }

    $consigneFR = @'
Interprète en français les données de facture ci-dessous.

Présente :
1. Le fournisseur et le numéro de facture.
2. La date de facture et la date d'échéance au format JJ/MM/AAAA.
3. Les montants HT, TVA et TTC ainsi que la devise.
4. Les informations manquantes ou incohérentes.

Utilise uniquement les données fournies et n'invente aucune valeur.

Données extraites :
'@
    $consigneEN = @'
Interpret the invoice data below in English.

Present:
1. The supplier and the invoice number.
2. The invoice date and the due date in DD/MM/YYYY format.
3. The amounts excluding tax, the VAT, and the amounts including tax, along with the currency.
4. Any missing or inconsistent information.

Use only the data provided and do not invent any values.

Extracted data:
'@
}

$Modeles = @($Modeles | Select-Object -Unique)
$Langues = @($Langues | ForEach-Object { $_.ToUpperInvariant() } | Select-Object -Unique)
$modelesLocaux = (Invoke-Ollama -Chemin '/api/tags').models
$modelesParNom = @{}
foreach ($local in $modelesLocaux) {
    $modelesParNom[$local.name] = $local
}
$absents = @($Modeles | Where-Object { -not $modelesParNom.ContainsKey($_) })
if ($absents.Count -gt 0) {
    $commandes = ($absents | ForEach-Object { "ollama pull $_" }) -join [Environment]::NewLine
    throw ("Modèles absents. Télécharge-les, puis relance l'essai :" + [Environment]::NewLine + $commandes)
}

$dossierBase = New-Item -ItemType Directory -Path $DossierSortie -Force
$identifiant = '{0}-{1}-{2}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $Essai, [guid]::NewGuid().ToString('N').Substring(0, 8)
$dossier = New-Item -ItemType Directory -Path (Join-Path $dossierBase.FullName $identifiant)
$csv = Join-Path $dossier.FullName 'mesures.csv'

$options = @{
    temperature = 0
    seed = 42
    num_ctx = $Contexte
    num_predict = $MaxTokens
}
$configuration = [ordered]@{
    essai = $Essai
    modeles = @($Modeles | ForEach-Object {
        [ordered]@{
            nom = $_
            digest = $modelesParNom[$_].digest
            quantification = $modelesParNom[$_].details.quantization_level
        }
    })
    langues = $Langues
    repetitions = $Repetitions
    options = $options
    keep_alive = 0
}
[IO.File]::WriteAllText(
    (Join-Path $dossier.FullName 'configuration.json'),
    (ConvertTo-Json -InputObject $configuration -Depth 8),
    $utf8
)

$enTetes = @{ FR = $consigneFR; EN = $consigneEN }
$ligne = [string][char]10
$separateur = if ($Essai -eq 'PHP') { $ligne + $ligne } else { $ligne }
$numero = 0

if ($null -ne $fonctionsPhp) {
    foreach ($langue in $Langues) {
        foreach ($modele in $Modeles) {
            for ($repetition = 1; $repetition -le $Repetitions; $repetition++) {
                $historiqueMessages = @()
                for ($idx = 0; $idx -lt $fonctionsPhp.Count; $idx++) {
                    $fn = $fonctionsPhp[$idx]
                    $estDerniere = ($idx -eq ($fonctionsPhp.Count - 1))
                    $numero++
                    $promptFn = $enTetes[$langue].Replace([string][char]13, '') + $separateur + $fn.Code
                    $nomPrompt = 'prompt-{0}-{1}.txt' -f $langue.ToLowerInvariant(), $fn.Nom
                    [IO.File]::WriteAllText((Join-Path $dossier.FullName $nomPrompt), $promptFn, $utf8)

                    Write-Host ("Essai {0} | {1} | {2} | {3} ({4}) | passage {5}" -f $numero, $modele, $langue, $fn.Nom, $ModeConversation, $repetition)

                    if ($ModeConversation -eq 'Isoler') {
                        $charges = @( (Invoke-Ollama -Chemin '/api/ps').models | ForEach-Object { $_.name } )
                        if ($charges -contains $modele) {
                            Invoke-Ollama -Chemin '/api/generate' -Corps @{
                                model = $modele
                                stream = $false
                                keep_alive = 0
                            } | Out-Null
                        }
                        $resultat = Invoke-Ollama -Chemin '/api/generate' -Corps @{
                            model = $modele
                            prompt = $promptFn
                            stream = $false
                            keep_alive = 0
                            options = $options
                        }
                    } else {
                        $keepAlive = if ($estDerniere) { 0 } else { '5m' }
                        if ($ModeConversation -eq 'Partage') {
                            $historiqueMessages += @{ role = 'user'; content = $promptFn }
                            $messagesEnvoi = $historiqueMessages
                        } else {
                            $messagesEnvoi = @(@{ role = 'user'; content = $promptFn })
                        }
                        $resultat = Invoke-Ollama -Chemin '/api/chat' -Corps @{
                            model = $modele
                            messages = $messagesEnvoi
                            stream = $false
                            keep_alive = $keepAlive
                            options = $options
                        }
                        if ($ModeConversation -eq 'Partage' -and $null -ne $resultat.message) {
                            $historiqueMessages += @{ role = 'assistant'; content = [string]$resultat.message.content }
                        }
                    }

                    $nomReponse = '{0:D3}-{1}-{2}-{3}.json' -f $numero, ($modele -replace '[^a-zA-Z0-9._-]', '-'), $langue, $fn.Nom
                    [IO.File]::WriteAllText(
                        (Join-Path $dossier.FullName $nomReponse),
                        (ConvertTo-Json -InputObject $resultat -Depth 12),
                        $utf8
                    )

                    $debit = if ($resultat.eval_duration -gt 0) {
                        [math]::Round([double]$resultat.eval_count * 1e9 / [double]$resultat.eval_duration, 2)
                    }
                    [pscustomobject][ordered]@{
                        Essai = $Essai
                        Fonction = $fn.Nom
                        ModeConversation = $ModeConversation
                        Modele = $modele
                        Langue = $langue
                        Passage = $repetition
                        TokensEntree = $resultat.prompt_eval_count
                        TokensSortie = $resultat.eval_count
                        TokensParSeconde = $debit
                        Chargement_s = ConvertTo-Secondes $resultat.load_duration
                        TraitementPrompt_s = ConvertTo-Secondes $resultat.prompt_eval_duration
                        Generation_s = ConvertTo-Secondes $resultat.eval_duration
                        Total_s = ConvertTo-Secondes $resultat.total_duration
                        Fin = $resultat.done_reason
                        Prompt = $nomPrompt
                        Reponse = $nomReponse
                    } | Export-Csv -LiteralPath $csv -Append -UseCulture -NoTypeInformation -Encoding utf8
                }
            }
        }
    }
    Import-Csv -LiteralPath $csv -UseCulture |
        Format-Table Modele, Fonction, ModeConversation, TokensEntree, TokensSortie, TokensParSeconde, Chargement_s, Total_s, Fin -AutoSize
} else {
    foreach ($langue in $Langues) {
        $prompt = $enTetes[$langue].Replace([string][char]13, '') + $separateur + $contenu
        $nomPrompt = 'prompt-{0}.txt' -f $langue.ToLowerInvariant()
        [IO.File]::WriteAllText((Join-Path $dossier.FullName $nomPrompt), $prompt, $utf8)

        foreach ($modele in $Modeles) {
            for ($repetition = 1; $repetition -le $Repetitions; $repetition++) {
                $numero++
                Write-Host ("Essai {0} | {1} | {2} | passage {3}" -f $numero, $modele, $langue, $repetition)

                $charges = @( (Invoke-Ollama -Chemin '/api/ps').models | ForEach-Object { $_.name } )
                if ($charges -contains $modele) {
                    Invoke-Ollama -Chemin '/api/generate' -Corps @{
                        model = $modele
                        stream = $false
                        keep_alive = 0
                    } | Out-Null
                }

                $resultat = Invoke-Ollama -Chemin '/api/generate' -Corps @{
                    model = $modele
                    prompt = $prompt
                    stream = $false
                    keep_alive = 0
                    options = $options
                }

                $nomReponse = '{0:D3}-{1}-{2}.json' -f $numero, ($modele -replace '[^a-zA-Z0-9._-]', '-'), $langue
                [IO.File]::WriteAllText(
                    (Join-Path $dossier.FullName $nomReponse),
                    (ConvertTo-Json -InputObject $resultat -Depth 12),
                    $utf8
                )

                $debit = if ($resultat.eval_duration -gt 0) {
                    [math]::Round([double]$resultat.eval_count * 1e9 / [double]$resultat.eval_duration, 2)
                }
                [pscustomobject][ordered]@{
                    Essai = $Essai
                    Modele = $modele
                    Langue = $langue
                    Passage = $repetition
                    TokensEntree = $resultat.prompt_eval_count
                    TokensSortie = $resultat.eval_count
                    TokensParSeconde = $debit
                    Chargement_s = ConvertTo-Secondes $resultat.load_duration
                    TraitementPrompt_s = ConvertTo-Secondes $resultat.prompt_eval_duration
                    Generation_s = ConvertTo-Secondes $resultat.eval_duration
                    Total_s = ConvertTo-Secondes $resultat.total_duration
                    Fin = $resultat.done_reason
                    Prompt = $nomPrompt
                    Reponse = $nomReponse
                } | Export-Csv -LiteralPath $csv -Append -UseCulture -NoTypeInformation -Encoding utf8
            }
        }
    }

    Import-Csv -LiteralPath $csv -UseCulture |
        Format-Table Modele, Langue, Passage, TokensEntree, TokensSortie, TokensParSeconde, Chargement_s, Total_s, Fin -AutoSize
}
Write-Host "Résultats : $($dossier.FullName)"
