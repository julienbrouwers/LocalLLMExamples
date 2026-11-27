<#
.SYNOPSIS
    Rejoue l'article Episode 3 (Hashnode) de bout en bout et vérifie chaque résultat annoncé.

.DESCRIPTION
    Chaque étape est une assertion : OK, ECART (le résultat contredit l'article) ou NON_EXECUTE
    (prérequis absent). Les mesures variables (chronos) sont relevées en INFO, sans assertion.
    Le script relève l'état initial (Ollama lancé ou non, variables utilisateur OLLAMA_*, règles
    pare-feu, conteneurs) et le restaure à la fin, même en cas d'erreur.

    Sections : 1 état initial (Desktop) · 2 Palier 1 option A · 3 OLLAMA_ORIGINS · 4 mode page blanche
    (pipe) · 5 OLLAMA_NO_CLOUD · 6 Palier 1 option B · 7 Palier 2 (-Admin) · 8 Palier 3 Docker.
    Durée : 8 à 12 minutes. Ne lancez rien d'autre sur Ollama ou Docker pendant ce temps.

.PARAMETER Admin
    Rejoue aussi le Palier 2 (pare-feu) dans une session élevée : Windows affiche une invite UAC.
    Ne s'exécute pas si des règles Ollama-Block-* existent déjà (elles sont préservées).
.PARAMETER CloneNeuf
    Clone la branche dans un dossier temporaire neuf et rejoue depuis ce clone.
.PARAMETER SansDocker
    Saute le Palier 3.

.EXAMPLE
    .\rejouer-article.ps1
.EXAMPLE
    .\rejouer-article.ps1 -CloneNeuf -Admin
#>
param(
    [string]$Modele = "qwen2.5:3b",
    [switch]$Admin,
    [switch]$CloneNeuf,
    [string]$Branche = "episode-3-llmlocaux",
    [string]$Depot = "https://github.com/julienbrouwers/LocalLLMExamples.git",
    [switch]$SansDocker,
    [string]$Rapport = "",
    # Usage interne : partie élevée du Palier 2
    [switch]$PartieAdmin,
    [string]$JournalServeur = "",
    [string]$SortieJson = ""
)

$ErrorActionPreference = "Continue"
$Image = "ollama/ollama@sha256:d00473cb58f0082c07cd6ed0d326a8a86f443ab69c51f8fc2b1a41687d45c661"
$IpOllamaCom = "34.36.133.15"

# --------------------------------------------------------------------------- outils communs
$Resultats = New-Object System.Collections.Generic.List[object]
function Add-Resultat([string]$Section, [string]$Etape, [string]$Statut, [string]$Detail) {
    $Resultats.Add([pscustomobject]@{ Section = $Section; Etape = $Etape; Statut = $Statut; Detail = $Detail })
    $couleur = switch ($Statut) { "OK" { "Green" } "ECART" { "Red" } "INFO" { "Cyan" } default { "Yellow" } }
    Write-Host ("[{0}] {1} | {2} : {3}" -f $Statut, $Section, $Etape, $Detail) -ForegroundColor $couleur
}
function Test-Assertion([string]$Section, [string]$Etape, [bool]$Condition, [string]$Detail) {
    Add-Resultat $Section $Etape $(if ($Condition) { "OK" } else { "ECART" }) $Detail
}
function Stop-Ollama { Stop-Process -Name "ollama app", "ollama", "llama-server" -Force -ErrorAction SilentlyContinue; Start-Sleep 3 }
function Wait-Ecoute([int]$Max = 45) {
    $t = 0
    while ($t -lt $Max -and -not (Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue)) { Start-Sleep 1; $t++ }
    [bool](Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue)
}
function Get-ConnexionsDistantes {
    $ids = @(Get-Process -Name "ollama*", "llama-server" -ErrorAction SilentlyContinue).Id
    if (-not $ids) { return @() }
    @(Get-NetTCPConnection -OwningProcess $ids -ErrorAction SilentlyContinue |
        Where-Object { $_.State -ne "Listen" -and $_.RemoteAddress -notin "127.0.0.1", "0.0.0.0", "::", "::1" })
}
function Watch-Connexions([int]$Secondes) {
    $vu = @{}
    for ($i = 0; $i -lt $Secondes; $i++) {
        foreach ($c in Get-ConnexionsDistantes) {
            $nom = (Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue).ProcessName
            $vu["$nom -> $($c.RemoteAddress):$($c.RemotePort)"] = 1
        }
        Start-Sleep 1
    }
    @($vu.Keys)
}
function Wait-SansConnexion([int]$Max = 300) {
    $t = 0
    while ($t -lt $Max -and @(Get-ConnexionsDistantes).Count) { Start-Sleep 5; $t += 5 }
    $t
}
function Invoke-Verificateur([string[]]$ArgsSup = @()) {
    $sortie = & python -X utf8 .\fonctions\verifier_isolation_ollama.py --modele $Modele --json @ArgsSup 2>$null
    $code = $LASTEXITCODE
    $json = $null
    try { $json = ($sortie -join "`n") | ConvertFrom-Json } catch { }
    [pscustomobject]@{ Json = $json; Code = $code }
}
function Get-Etat($v, [string]$Controle) { if ($v.Json) { $v.Json.controles.$Controle.etat } else { "illisible" } }
function Start-ServeurDurci([string]$Journal) {
    . (Join-Path $PSScriptRoot "securiser-ollama-windows.ps1") -Action EnvSession | Out-Null
    Remove-Item $Journal, "$Journal.out" -ErrorAction SilentlyContinue
    Start-Process ollama -ArgumentList serve -WindowStyle Hidden -RedirectStandardError $Journal -RedirectStandardOutput "$Journal.out" | Out-Null
    Wait-Ecoute | Out-Null
}
function Start-Desktop { explorer.exe "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe"; Wait-Ecoute | Out-Null; Start-Sleep 3 }
function Read-Partage([string]$Chemin) {
    # Lecture tolérant un fichier encore ouvert en écriture par 'ollama serve'
    $fs = [IO.File]::Open($Chemin, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try { (New-Object IO.StreamReader($fs)).ReadToEnd() } finally { $fs.Dispose() }
}
function Get-Texte($o) { (($o | ForEach-Object { "$_" }) -join " ") -replace '\x1b\[[0-9;?]*[a-zA-Z]', '' -replace '[⠀-⣿]', '' }

# --------------------------------------------------------------------------- partie élevée (Palier 2)
if ($PartieAdmin) {
    Set-Location $PSScriptRoot
    $s = "7. Palier 2"
    try {
        $avant = @(Get-NetFirewallRule -Name "Ollama-Block-*" -ErrorAction SilentlyContinue)
        if ($avant.Count) {
            Add-Resultat $s "règles préexistantes" "NON_EXECUTE" "$($avant.Count) règle(s) Ollama-Block-* déjà présente(s) : préservées, essai non lancé"
        } else {
            ollama pull tinyllama *> $null
            Test-Assertion $s "témoin pull avant blocage" ($LASTEXITCODE -eq 0) "exit $LASTEXITCODE (attendu 0)"
            .\securiser-ollama-windows.ps1 -Action Bloquer | Out-Null
            $actives = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name "Ollama-Block-*" -ErrorAction SilentlyContinue |
                Where-Object { $_.Enabled -eq "True" -and $_.Action -eq "Block" -and $_.Direction -eq "Outbound" })
            Test-Assertion $s "Bloquer : règles ActiveStore" ($actives.Count -eq 3) "$($actives.Count)/3 : $($actives.Name -join ', ')"
            $sortiePull = Get-Texte (ollama pull tinyllama 2>&1)
            $codePull = $LASTEXITCODE
            Test-Assertion $s "pull bloqué" ($codePull -ne 0) "exit $codePull ; $(if ($sortiePull -match 'wsarecv|connectex') { $Matches[0] } else { $sortiePull.Substring(0, [Math]::Min(120, $sortiePull.Length)) })"
            $v = Invoke-Verificateur @("--strict", "--journal-serveur", $JournalServeur)
            Test-Assertion $s "--strict" ($v.Code -eq 0 -and $v.Json.statut -eq "PASS") "statut=$($v.Json.statut) exit=$($v.Code) ; pare-feu=$(Get-Etat $v 'pare_feu_sortant_bloque') ; durcissement=$(Get-Etat $v 'variables_durcissement')"
        }
    } finally {
        if (-not $avant.Count) {
            .\securiser-ollama-windows.ps1 -Action Debloquer | Out-Null
            $apres = @(Get-NetFirewallRule -Name "Ollama-Block-*" -ErrorAction SilentlyContinue).Count
            Test-Assertion $s "Debloquer" ($apres -eq 0) "$apres règle(s) restante(s) (attendu 0)"
            ollama pull tinyllama *> $null
            Test-Assertion $s "pull rétabli" ($LASTEXITCODE -eq 0) "exit $LASTEXITCODE (attendu 0)"
        }
        $Resultats | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $SortieJson -Encoding UTF8
    }
    exit 0
}

# --------------------------------------------------------------------------- clone neuf
if ($CloneNeuf) {
    $dossier = Join-Path $env:TEMP ("rejeu-article-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    git clone -q -b $Branche $Depot $dossier 2>&1 | Out-Null
    if (-not (Test-Path (Join-Path $dossier "rejouer-article.ps1"))) { Write-Error "Clone impossible ou script absent : $dossier"; exit 1 }
    Write-Host "Clone neuf : $dossier ($(git -C $dossier log --oneline -1))"
    $params = @{ Modele = $Modele; SansDocker = $SansDocker; Admin = $Admin }
    if ($Rapport) { $params.Rapport = $Rapport } else { $params.Rapport = Join-Path (Get-Location) ("rejeu-article-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".md") }
    & (Join-Path $dossier "rejouer-article.ps1") @params
    exit $LASTEXITCODE
}

# --------------------------------------------------------------------------- préparation
Set-Location $PSScriptRoot
if (-not $Rapport) { $Rapport = Join-Path $PSScriptRoot ("rejeu-article-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".md") }
$Travail = Join-Path $env:TEMP ("rejeu-travail-" + (Get-Date -Format "yyyyMMddHHmmss"))
New-Item -ItemType Directory -Path $Travail -Force | Out-Null

$etatInitial = [pscustomobject]@{
    DesktopLance = [bool](Get-Process -Name "ollama app" -ErrorAction SilentlyContinue)
    VarsUtilisateur = @([Environment]::GetEnvironmentVariables("User").GetEnumerator() | Where-Object { $_.Key -like "OLLAMA*" } | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "; "
    Regles = @(Get-NetFirewallRule -Name "Ollama-Block-*" -ErrorAction SilentlyContinue).Count
    Commit = (git log --oneline -1 2>$null)
}
$s = "0. Environnement"
Add-Resultat $s "commit" "INFO" "$($etatInitial.Commit)"
Add-Resultat $s "versions" "INFO" "$((ollama --version 2>&1 | Select-Object -Last 1)) | Python $((python --version 2>&1)) | Docker $(docker version --format '{{.Server.Version}}' 2>$null)"
Add-Resultat $s "état initial" "INFO" "Desktop lancé=$($etatInitial.DesktopLance) ; variables utilisateur OLLAMA_*='$($etatInitial.VarsUtilisateur)' ; règles Ollama-Block-*=$($etatInitial.Regles)"
if ($etatInitial.VarsUtilisateur) { Add-Resultat $s "état par défaut" "INFO" "des variables OLLAMA_* existent déjà au niveau utilisateur : les attentes 'sortie de boîte' peuvent différer" }

try {
    # ======================================================================= 1. état initial (Desktop)
    $s = "1. Sortie de boîte"
    Stop-Ollama; Remove-Item Env:OLLAMA_* -ErrorAction SilentlyContinue
    Start-Desktop
    $ecoutes = @(Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue).LocalAddress | Sort-Object -Unique
    Test-Assertion $s "écoute 11434 en loopback" (($ecoutes -join ",") -eq "127.0.0.1") "adresses : $($ecoutes -join ', ')"
    $vues = Watch-Connexions 15
    $versOllamaCom = @($vues | Where-Object { $_ -like "*$($IpOllamaCom):443" })
    Test-Assertion $s "connexions vers ollama.com au démarrage" ($versOllamaCom.Count -ge 1) "vues en 15 s : $(if ($vues) { $vues -join ' ; ' } else { 'aucune' })"
    $attente = Wait-SansConnexion
    Add-Resultat $s "fermeture des connexions" "INFO" "après ~$attente s d'attente supplémentaire"
    $v = Invoke-Verificateur
    Test-Assertion $s "audit : verdict" ($v.Json.statut -eq "PARTIAL") "statut=$($v.Json.statut) (attendu PARTIAL)"
    Test-Assertion $s "audit : zéro connexion pendant le test" ((Get-Etat $v "zero_connexion_distante") -eq "reussi") "$(Get-Etat $v 'zero_connexion_distante')"
    Test-Assertion $s "audit : durcissement serveur" ((Get-Etat $v "variables_durcissement") -eq "echec") "$(Get-Etat $v 'variables_durcissement') (attendu echec, 'cloud disabled: false')"
    Test-Assertion $s "audit : marqueur absent du journal" ((Get-Etat $v "zero_canari_dans_server_log") -eq "reussi") "$(Get-Etat $v 'zero_canari_dans_server_log')"
    Add-Resultat $s "audit : chrono" "INFO" "$($v.Json.mesures_inference.total_s) s ($($v.Json.mesures_inference.mode))"
    $dossierOllama = Join-Path $env:LOCALAPPDATA "Ollama"
    Test-Assertion $s "upgrade.log présent" (Test-Path (Join-Path $dossierOllama "upgrade.log")) "$(Join-Path $dossierOllama 'upgrade.log')"
    $py = Join-Path $Travail "tables.py"
    @'
import os, sqlite3
p = os.path.expandvars(r"%LOCALAPPDATA%\Ollama\db.sqlite").replace("\\", "/")
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
t = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
lignes = sum(c.execute(f"select count(*) from {x}").fetchone()[0] for x in ("chats", "messages") if x in t)
print(",".join(sorted(t)) + "|" + str(lignes))
'@ | Set-Content -LiteralPath $py -Encoding UTF8
    $tables = (python -I $py 2>$null)
    $nomsTables = ($tables -split "\|")[0]
    Test-Assertion $s "db.sqlite : tables chats/messages/attachments" ($nomsTables -match "chats" -and $nomsTables -match "messages" -and $nomsTables -match "attachments") "$nomsTables"
    Stop-Ollama

    # ======================================================================= 2. Palier 1, option A
    $s = "2. Palier 1 (A)"
    $journal = Join-Path $Travail "ollama-serve.log"
    Start-ServeurDurci $journal
    $cloud = (Select-String -LiteralPath $journal -Pattern 'Ollama cloud disabled: (true|false)' | Select-Object -Last 1).Matches.Groups[1].Value
    Test-Assertion $s "serveur : cloud disabled" ($cloud -eq "true") "cloud disabled: $cloud"
    $vues = Watch-Connexions 20
    Test-Assertion $s "aucune connexion distante en 20 s" ($vues.Count -eq 0) "$(if ($vues) { $vues -join ' ; ' } else { 'aucune' })"
    $v = Invoke-Verificateur @("--journal-serveur", $journal)
    Test-Assertion $s "audit : verdict" ($v.Json.statut -eq "PARTIAL") "statut=$($v.Json.statut) (attendu PARTIAL, pare-feu absent)"
    Test-Assertion $s "audit : durcissement confirmé côté serveur" ((Get-Etat $v "variables_durcissement") -eq "reussi") "$(Get-Etat $v 'variables_durcissement')"
    Test-Assertion $s "audit : marqueur absent du journal" ((Get-Etat $v "zero_canari_dans_server_log") -eq "reussi") "$(Get-Etat $v 'zero_canari_dans_server_log')"
    $v = Invoke-Verificateur
    Test-Assertion $s "sans --journal-serveur : non concluant" ((Get-Etat $v "variables_durcissement") -eq "non_concluant") "durcissement=$(Get-Etat $v 'variables_durcissement') ; journal=$(Get-Etat $v 'zero_canari_dans_server_log')"
    foreach ($i in 1..3) {
        $vc = Invoke-Verificateur @("--journal-serveur", $journal, "--keep-alive", "5m")
        Add-Resultat $s "chrono $i" "INFO" "$($vc.Json.mesures_inference.total_s) s ($($vc.Json.mesures_inference.mode)) ; $($vc.Json.echantillonnage_tcp.nb_echantillons) relevés sur $($vc.Json.echantillonnage_tcp.fenetre_s) s"
    }

    # ======================================================================= 3. OLLAMA_ORIGINS (serveur durci)
    $s = "3. OLLAMA_ORIGINS"
    $essais = @(
        @{ Nom = "GET depuis une origine externe"; Uri = "http://127.0.0.1:11434/api/tags"; Methode = "Get"; Entetes = @{ Origin = "https://evil.example" }; Corps = $null },
        @{ Nom = "POST text/plain (sans préflight)"; Uri = "http://127.0.0.1:11434/api/show"; Methode = "Post"; Entetes = @{ Origin = "https://evil.example" }; Corps = '{"model":"' + $Modele + '"}' },
        @{ Nom = "Host étranger (DNS rebinding)"; Uri = "http://127.0.0.1:11434/api/tags"; Methode = "Get"; Entetes = @{ Host = "attacker.example:11434" }; Corps = $null }
    )
    foreach ($e in $essais) {
        $code = 0
        try {
            $p = @{ Uri = $e.Uri; Method = $e.Methode; Headers = $e.Entetes; UseBasicParsing = $true }
            if ($e.Corps) { $p.Body = $e.Corps; $p.ContentType = "text/plain" }
            $r = Invoke-WebRequest @p; $code = [int]$r.StatusCode
        } catch { $code = [int]$_.Exception.Response.StatusCode }
        Test-Assertion $s $e.Nom ($code -eq 403) "HTTP $code (attendu 403)"
    }

    # ======================================================================= 4. page blanche (pipe)
    $s = "4. Page blanche (pipe)"
    $historique = Join-Path $env:USERPROFILE ".ollama\history"
    $lignesAvant = if (Test-Path $historique) { (Get-Content $historique | Measure-Object -Line).Lines } else { 0 }
    $tailleAvant = (Get-Item $journal).Length
    $jeton = "MEMO-FICTIF-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
    $memo = Join-Path $Travail "memo-fictif.txt"
    "Memo fictif. Reference $jeton. Budget fictif 12 000 EUR. Decision fictive : reporter le projet." | Set-Content -LiteralPath $memo -Encoding UTF8
    $reponse = Get-Texte (Get-Content -Raw $memo | ollama run $Modele "Summarize in 3 bullets:" --keepalive 0 2>$null)
    Test-Assertion $s "réponse obtenue" ($reponse.Trim().Length -gt 0) "$($reponse.Trim().Substring(0, [Math]::Min(90, $reponse.Trim().Length)))..."
    Start-Sleep 2
    $charges = @(ollama ps 2>$null | Select-Object -Skip 1 | Where-Object { $_.Trim() })
    Test-Assertion $s "modèle déchargé (--keepalive 0)" ($charges.Count -eq 0) "ollama ps : $(if ($charges) { $charges -join ' / ' } else { 'vide' })"
    $lignesApres = if (Test-Path $historique) { (Get-Content $historique | Measure-Object -Line).Lines } else { 0 }
    Test-Assertion $s "historique CLI inchangé" ($lignesApres -eq $lignesAvant) "$lignesAvant -> $lignesApres lignes"
    $texteJournal = Read-Partage $journal
    $ecrit = if ($texteJournal.Length -gt $tailleAvant) { $texteJournal.Substring([int]$tailleAvant) } else { "" }
    Test-Assertion $s "jeton et consigne absents du journal" (-not $ecrit.Contains($jeton) -and -not $ecrit.Contains("Summarize")) "$($ecrit.Length) caractères écrits pendant l'essai"
    Stop-Ollama; Remove-Item Env:OLLAMA_* -ErrorAction SilentlyContinue

    # ======================================================================= 5. OLLAMA_NO_CLOUD
    $s = "5. OLLAMA_NO_CLOUD"
    foreach ($avec in $false, $true) {
        Remove-Item Env:OLLAMA_* -ErrorAction SilentlyContinue
        if ($avec) { $env:OLLAMA_NO_CLOUD = "1" }
        $j = Join-Path $Travail ("nocloud-$avec.log")
        Start-Process ollama -ArgumentList serve -WindowStyle Hidden -RedirectStandardError $j -RedirectStandardOutput "$j.out" | Out-Null
        Wait-Ecoute | Out-Null
        $vues = @(Watch-Connexions 20 | Where-Object { $_ -like "*:443" })
        if ($avec) { Test-Assertion $s "serve AVEC NO_CLOUD : aucune connexion" ($vues.Count -eq 0) "$(if ($vues) { $vues -join ' ; ' } else { 'aucune' })" }
        else { Test-Assertion $s "serve SANS NO_CLOUD : connexion vers ollama.com" (@($vues | Where-Object { $_ -like "*$IpOllamaCom*" }).Count -ge 1) "$(if ($vues) { $vues -join ' ; ' } else { 'aucune' })" }
        Stop-Ollama
    }
    Remove-Item Env:OLLAMA_* -ErrorAction SilentlyContinue

    # ======================================================================= 6. Palier 1, option B
    $s = "6. Palier 1 (B)"
    .\securiser-ollama-windows.ps1 -Action EnvUtilisateur | Out-Null
    Remove-Item Env:OLLAMA_* -ErrorAction SilentlyContinue
    Start-Desktop
    $vues = Watch-Connexions 15
    $cloud = (Select-String -LiteralPath (Join-Path $env:LOCALAPPDATA "Ollama\server.log") -Pattern 'Ollama cloud disabled: (true|false)' | Select-Object -Last 1).Matches.Groups[1].Value
    Test-Assertion $s "serveur Desktop : cloud disabled" ($cloud -eq "true") "cloud disabled: $cloud"
    Test-Assertion $s "Desktop contacte encore ollama.com" (@($vues | Where-Object { $_ -like "*$($IpOllamaCom):443" }).Count -ge 1) "$(if ($vues) { $vues -join ' ; ' } else { 'aucune' })"
    Wait-SansConnexion | Out-Null
    $v = Invoke-Verificateur
    Test-Assertion $s "audit : durcissement confirmé côté serveur" ((Get-Etat $v "variables_durcissement") -eq "reussi") "$(Get-Etat $v 'variables_durcissement')"
    . .\securiser-ollama-windows.ps1 -Action Reinitialiser | Out-Null
    $restantes = @([Environment]::GetEnvironmentVariables("User").GetEnumerator() | Where-Object { $_.Key -like "OLLAMA*" } | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "; "
    Test-Assertion $s "Reinitialiser : variables d'origine" ($restantes -eq $etatInitial.VarsUtilisateur) "'$restantes' (initial : '$($etatInitial.VarsUtilisateur)')"
    Stop-Ollama

    # ======================================================================= 7. Palier 2 (admin)
    $s = "7. Palier 2"
    if (-not $Admin) {
        Add-Resultat $s "pare-feu" "NON_EXECUTE" "relancer avec -Admin (invite UAC)"
    } else {
        $journalAdmin = Join-Path $Travail "ollama-serve-admin.log"
        Start-ServeurDurci $journalAdmin
        $sortie = Join-Path $Travail "palier2.json"
        try {
            Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
                "-PartieAdmin", "-Modele", $Modele, "-JournalServeur", "`"$journalAdmin`"", "-SortieJson", "`"$sortie`""
            if (Test-Path $sortie) {
                # PowerShell 5.1 : ConvertFrom-Json renvoie le tableau comme un seul objet ; l'affecter avant de l'énumérer
                $lus = Get-Content -Raw -LiteralPath $sortie -Encoding UTF8 | ConvertFrom-Json
                foreach ($r in $lus) { Add-Resultat $r.Section $r.Etape $r.Statut $r.Detail }
            } else { Add-Resultat $s "session élevée" "NON_EXECUTE" "aucun résultat (invite UAC refusée ?)" }
        } catch { Add-Resultat $s "session élevée" "NON_EXECUTE" "$($_.Exception.Message)" }
        Stop-Ollama; Remove-Item Env:OLLAMA_* -ErrorAction SilentlyContinue
    }

    # ======================================================================= 8. Palier 3 (Docker)
    $s = "8. Palier 3 (Docker)"
    if ($SansDocker -or -not (docker version --format '{{.Server.Version}}' 2>$null)) {
        Add-Resultat $s "Docker" "NON_EXECUTE" "Docker absent ou -SansDocker"
    } else {
        docker rm -f ollama-airgap ep3-hard ep3-vide 2>$null | Out-Null
        $env:EP3_OLLAMA_IMAGE = $Image
        $env:EP3_MODELS = (Resolve-Path -LiteralPath "$env:USERPROFILE\.ollama\models").Path
        docker image inspect $Image *> $null; if ($LASTEXITCODE -ne 0) { docker pull $Image *> $null }
        docker compose -f .\docker-compose.offline.yml --profile airgap up --pull never -d ollama-airgap *> $null
        Test-Assertion $s "up --pull never" ($LASTEXITCODE -eq 0) "exit $LASTEXITCODE"
        Start-Sleep 4
        $insp = docker inspect ollama-airgap --format '{{.HostConfig.NetworkMode}} {{json .HostConfig.PortBindings}} {{range .Mounts}}{{.Destination}} RW={{.RW}}{{end}}'
        Test-Assertion $s "inspect" ("$insp" -eq "none {} /models RW=false") "$insp"
        $interfaces = @(docker exec ollama-airgap cat /proc/net/dev | Select-Object -Skip 2 | ForEach-Object { ($_ -split ":")[0].Trim() })
        Test-Assertion $s "/proc/net/dev : lo seule" (($interfaces -join ",") -eq "lo") "interfaces : $($interfaces -join ', ')"
        $ecriture = Get-Texte (docker exec ollama-airgap sh -c 'touch /models/x 2>&1; true')
        Test-Assertion $s "/models en lecture seule" ($ecriture -match "Read-only file system") "$ecriture"
        $nb = @(docker exec ollama-airgap ollama list | Select-Object -Skip 1 | Where-Object { $_.Trim() }).Count
        Test-Assertion $s "ollama list" ($nb -ge 1) "$nb modèle(s)"
        $pull = Get-Texte (docker exec ollama-airgap ollama pull tinyllama 2>&1); $codePull = $LASTEXITCODE
        Test-Assertion $s "pull refusé" ($codePull -ne 0 -and $pull -match "network is unreachable") "exit $codePull"
        docker exec ollama-airgap bash -c "exec 3<>/dev/tcp/$IpOllamaCom/443" *> $null
        Test-Assertion $s "TCP direct vers une IP refusé" ($LASTEXITCODE -ne 0) "exit $LASTEXITCODE"
        $versionConteneur = Get-Texte (docker exec ollama-airgap ollama --version 2>&1)
        Test-Assertion $s "version Ollama de l'image" ($versionConteneur -match "0\.23\.2") "$versionConteneur"
        foreach ($i in 1..2) {
            $chrono = Measure-Command { $rep = Get-Texte (docker exec ollama-airgap ollama run $Modele "Reply with OK only." 2>$null) }
            Test-Assertion $s "inférence hors ligne $i" ($rep -match "OK") ("réponse '{0}' en {1:N1} s" -f $rep.Trim(), $chrono.TotalSeconds)
        }
        $sauve = @{ I = $env:EP3_OLLAMA_IMAGE; M = $env:EP3_MODELS }
        Remove-Item Env:EP3_* -ErrorAction SilentlyContinue
        $sansVar = Get-Texte (docker compose -f .\docker-compose.offline.yml --profile airgap down 2>&1); $codeSansVar = $LASTEXITCODE
        Test-Assertion $s "down sans EP3_* échoue" ($codeSansVar -ne 0 -and $sansVar -match "required variable") "exit $codeSansVar"
        $env:EP3_OLLAMA_IMAGE = $sauve.I; $env:EP3_MODELS = $sauve.M
        docker compose -f .\docker-compose.offline.yml --profile airgap down *> $null
        $reste = docker ps -a --filter name=ollama-airgap --format '{{.Names}}'
        Test-Assertion $s "down avec EP3_*" ($LASTEXITCODE -eq 0 -and -not $reste) "conteneur restant : '$reste'"

        docker run -d --name ep3-hard --network none --cap-drop ALL --security-opt no-new-privileges:true `
            --read-only --tmpfs /root/.ollama:rw,size=64m --tmpfs /tmp:rw,size=256m `
            -e OLLAMA_MODELS=/models -v "$($env:EP3_MODELS):/models:ro" $env:EP3_OLLAMA_IMAGE *> $null
        Start-Sleep 4
        $rep = Get-Texte (docker exec ep3-hard ollama run $Modele "Reply with OK only." 2>$null)
        Test-Assertion $s "variante durcie (cap-drop ALL, read-only)" ($rep -match "OK") "réponse '$($rep.Trim())'"
        docker rm -f ep3-hard *> $null

        $vide = Join-Path $Travail "modeles-vides"; New-Item -ItemType Directory -Path $vide -Force | Out-Null
        docker run -d --name ep3-vide --network none -e OLLAMA_MODELS=/models -v "$($vide):/models:ro" $env:EP3_OLLAMA_IMAGE *> $null
        Start-Sleep 5
        $journalVide = Get-Texte (docker logs ep3-vide 2>&1)
        Test-Assertion $s ":ro sur dossier vide : arrêt au démarrage" ($journalVide -match "mkdir /models/blobs: read-only file system") "$(docker ps -a --filter name=ep3-vide --format '{{.Status}}')"
        docker rm -f ep3-vide *> $null
    }
}
catch {
    Add-Resultat "!" "erreur inattendue" "ECART" "$($_.Exception.Message)"
}
finally {
    # ======================================================================= restauration
    Stop-Ollama
    Remove-Item Env:OLLAMA_*, Env:EP3_* -ErrorAction SilentlyContinue
    docker rm -f ollama-airgap ep3-hard ep3-vide 2>$null | Out-Null
    if (Test-Path (Join-Path $env:TEMP "ollama-ep3-env-backup.json")) { . .\securiser-ollama-windows.ps1 -Action Reinitialiser | Out-Null }
    if ($etatInitial.DesktopLance) { Start-Desktop }
    $varsFin = @([Environment]::GetEnvironmentVariables("User").GetEnumerator() | Where-Object { $_.Key -like "OLLAMA*" } | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "; "
    $reglesFin = @(Get-NetFirewallRule -Name "Ollama-Block-*" -ErrorAction SilentlyContinue).Count
    Test-Assertion "9. Restauration" "état initial rétabli" ($varsFin -eq $etatInitial.VarsUtilisateur -and $reglesFin -eq $etatInitial.Regles) "variables='$varsFin' ; règles=$reglesFin ; Desktop relancé=$($etatInitial.DesktopLance)"
    Remove-Item $Travail -Recurse -Force -ErrorAction SilentlyContinue

    # ======================================================================= rapport
    $nbOk = @($Resultats | Where-Object Statut -eq "OK").Count
    $nbEcart = @($Resultats | Where-Object Statut -eq "ECART").Count
    $nbNon = @($Resultats | Where-Object Statut -eq "NON_EXECUTE").Count
    $lignes = @("# Rejeu de l'article Episode 3", "", "$(Get-Date -Format 'yyyy-MM-dd HH:mm') · $($etatInitial.Commit)", "",
        "**$nbOk OK · $nbEcart ECART · $nbNon NON_EXECUTE**", "", "| Section | Étape | Statut | Détail |", "| --- | --- | --- | --- |")
    foreach ($r in $Resultats) { $lignes += "| $($r.Section) | $($r.Etape) | $($r.Statut) | $(($r.Detail -replace '\|', '/') -replace '\r?\n', ' ') |" }
    $lignes | Set-Content -LiteralPath $Rapport -Encoding UTF8
    Write-Host ""
    Write-Host "Bilan : $nbOk OK, $nbEcart ECART, $nbNon NON_EXECUTE. Rapport : $Rapport" -ForegroundColor $(if ($nbEcart) { "Red" } else { "Green" })
    if ($nbEcart) { exit 1 } else { exit 0 }
}
