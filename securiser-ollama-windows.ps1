<#
.SYNOPSIS
    Durcit, vérifie ou réinitialise l'isolation réseau d'Ollama sur Windows
    (variables d'environnement avec sauvegarde/restauration exacte + règles pare-feu Windows Defender).

.NOTES
    - Le serveur Ollama (`ollama serve` / `ollama app.exe`) ne lit les variables `OLLAMA_*`
      qu'à son démarrage.
    - `-Action EnvUtilisateur` sauvegarde les valeurs précédentes du profil utilisateur Windows
      dans `$env:TEMP\ollama-ep3-env-backup.json` avant modification ; `-Action Reinitialiser`
      restaure ces valeurs exactes (ou supprime les variables si elles n'existaient pas).
    - Les variables d'environnement ne coupent PAS la vérification des mises à jour d'Ollama Desktop
      (`ollama app.exe`, toutes les heures) : aucune variable `OLLAMA_*` ne le permet. Utilisez le réglage
      de mise à jour automatique de l'application, `ollama serve` sans l'application, ou `-Action Bloquer`.
    - `-Action Bloquer` bloque les chemins exacts des trois binaires réseau-capables d'Ollama :
      `ollama.exe` (`Ollama-Block-Outbound-LocalOnly`), `ollama app.exe` (`Ollama-Block-Outbound-App-LocalOnly`)
      et le moteur d'inférence `lib\ollama\llama-server.exe` (`Ollama-Block-Outbound-LlamaServer`),
      qui charge les poids et traite les prompts depuis Ollama 0.40.x.
    - `-Action Debloquer` supprime UNIQUEMENT les trois règles créées par ce script.

.EXAMPLE
    .\securiser-ollama-windows.ps1 -Action Statut
.EXAMPLE
    . .\securiser-ollama-windows.ps1 -Action EnvSession
.EXAMPLE
    .\securiser-ollama-windows.ps1 -Action EnvUtilisateur
.EXAMPLE
    . .\securiser-ollama-windows.ps1 -Action Reinitialiser
.EXAMPLE
    .\securiser-ollama-windows.ps1 -Action Bloquer
.EXAMPLE
    .\securiser-ollama-windows.ps1 -Action Debloquer
#>
param(
    [ValidateSet("Statut", "EnvSession", "EnvUtilisateur", "Reinitialiser", "Bloquer", "Debloquer")]
    [string]$Action = "Statut"
)

$NomRegleCli = "Ollama-Block-Outbound-LocalOnly"
$NomRegleApp = "Ollama-Block-Outbound-App-LocalOnly"
$NomRegleRunner = "Ollama-Block-Outbound-LlamaServer"
$NomsRegles = @($NomRegleCli, $NomRegleApp, $NomRegleRunner)
$FichierSauvegardeEnv = Join-Path $env:TEMP "ollama-ep3-env-backup.json"

$VariablesDurcissement = [ordered]@{
    "OLLAMA_HOST"              = "http://127.0.0.1:11434"
    "OLLAMA_NO_CLOUD"          = "1"
    "OLLAMA_MAX_LOADED_MODELS" = "1"
    "OLLAMA_KEEP_ALIVE"        = "5m"
}

function Test-EstAdmin {
    $identite = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identite)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-CheminsOllama {
    $chemins = [ordered]@{}
    $procCli = Get-Process -Name "ollama" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path
    $procApp = Get-Process -Name "ollama app" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path

    if ($procCli) { $chemins["cli"] = $procCli }
    else {
        $cmd = (Get-Command ollama -ErrorAction SilentlyContinue).Source
        $chemins["cli"] = if ($cmd) { $cmd } else { "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
    }

    if ($procApp) { $chemins["app"] = $procApp }
    else {
        $candidatApp = Join-Path (Split-Path -Parent $chemins["cli"]) "ollama app.exe"
        if (Test-Path -LiteralPath $candidatApp) { $chemins["app"] = $candidatApp }
    }

    # Moteur d'inférence (Ollama 0.40.x) : lib\ollama\llama-server.exe à côté de ollama.exe
    $procRunner = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path -like "*\lib\ollama\llama-server.exe" } |
        Select-Object -First 1 -ExpandProperty Path
    if ($procRunner) { $chemins["runner"] = $procRunner }
    else {
        $candidatRunner = Join-Path (Split-Path -Parent $chemins["cli"]) "lib\ollama\llama-server.exe"
        if (Test-Path -LiteralPath $candidatRunner) { $chemins["runner"] = $candidatRunner }
    }
    return $chemins
}

$NoteMisesAJour = "NOTE : ces variables ne coupent PAS la vérification des mises à jour d'Ollama Desktop (ollama app.exe contacte ollama.com au démarrage puis toutes les heures). Désactivez la mise à jour automatique dans l'application, utilisez 'ollama serve' sans l'application, ou '-Action Bloquer' (admin)."

$Chemins = Resolve-CheminsOllama

switch ($Action) {
    "EnvSession" {
        foreach ($cle in $VariablesDurcissement.Keys) {
            Set-Item -Path "Env:$cle" -Value $VariablesDurcissement[$cle]
        }
        Write-Output "Variables de durcissement appliquées pour cette session PowerShell :"
        Get-ChildItem Env:OLLAMA* | Format-Table Name, Value -AutoSize
        Write-Output "RAPPEL : Ollama ne lit ces variables qu'au démarrage du serveur. Quittez Ollama Desktop puis lancez le serveur depuis cette session (ou utilisez -Action EnvUtilisateur puis redémarrez Ollama)."
        Write-Output "Pour que verifier_isolation_ollama.py puisse lire le journal de ce serveur :"
        Write-Output "  Start-Process ollama -ArgumentList serve -NoNewWindow -RedirectStandardError .\ollama-serve.log -RedirectStandardOutput .\ollama-serve.out"
        Write-Output "  python -X utf8 .\fonctions\verifier_isolation_ollama.py --journal-serveur .\ollama-serve.log"
        Write-Output $NoteMisesAJour
    }

    "EnvUtilisateur" {
        if (-not (Test-Path -LiteralPath $FichierSauvegardeEnv)) {
            $etatInitial = [ordered]@{}
            foreach ($cle in $VariablesDurcissement.Keys) {
                $etatInitial[$cle] = [Environment]::GetEnvironmentVariable($cle, "User")
            }
            $etatInitial | ConvertTo-Json | Set-Content -LiteralPath $FichierSauvegardeEnv -Encoding UTF8
            Write-Output "Sauvegarde de l'état initial des variables utilisateur enregistrée : $FichierSauvegardeEnv"
        }
        foreach ($cle in $VariablesDurcissement.Keys) {
            [Environment]::SetEnvironmentVariable($cle, $VariablesDurcissement[$cle], "User")
            Set-Item -Path "Env:$cle" -Value $VariablesDurcissement[$cle]
        }
        Write-Output "Variables de durcissement enregistrées pour l'utilisateur Windows courant :"
        Get-ChildItem Env:OLLAMA* | Format-Table Name, Value -AutoSize
        Write-Output "Redémarrez maintenant Ollama pour que le serveur prenne en compte ces variables."
        Write-Output $NoteMisesAJour
    }

    "Reinitialiser" {
        if (Test-Path -LiteralPath $FichierSauvegardeEnv) {
            $sauvegarde = Get-Content -LiteralPath $FichierSauvegardeEnv -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($cle in $VariablesDurcissement.Keys) {
                $valeurAvant = $sauvegarde.$cle
                if ($null -eq $valeurAvant) {
                    Remove-Item -Path "Env:$cle" -ErrorAction SilentlyContinue
                    if ($null -ne [Environment]::GetEnvironmentVariable($cle, "User")) {
                        [Environment]::SetEnvironmentVariable($cle, $null, "User")
                    }
                } else {
                    Set-Item -Path "Env:$cle" -Value $valeurAvant
                    [Environment]::SetEnvironmentVariable($cle, [string]$valeurAvant, "User")
                }
            }
            Remove-Item -LiteralPath $FichierSauvegardeEnv -Force -ErrorAction SilentlyContinue
            Write-Output "Valeurs exactes d'origine restaurées dans la session et le profil utilisateur Windows (sauvegarde nettoyée)."
        } else {
            foreach ($cle in $VariablesDurcissement.Keys) {
                Remove-Item -Path "Env:$cle" -ErrorAction SilentlyContinue
            }
            Write-Output "Variables OLLAMA_* supprimées de la session courante uniquement."
            Write-Output "Aucune sauvegarde EnvUtilisateur trouvée : les variables persistantes du profil utilisateur Windows ont été préservées telles quelles."
        }

        $regles = Get-NetFirewallRule -Name $NomsRegles -ErrorAction SilentlyContinue
        if ($regles) {
            Write-Warning "Des règles pare-feu créées par ce script sont encore actives : lancez '.\securiser-ollama-windows.ps1 -Action Debloquer' en PowerShell Administrateur."
        }
        Write-Output "Redémarrez Ollama pour revenir à l'état d'origine."
    }

    "Bloquer" {
        if (-not (Test-EstAdmin)) {
            Write-Error "Action 'Bloquer' : relancez PowerShell en tant qu'Administrateur."
            exit 1
        }
        if (-not $Chemins["cli"] -or -not (Test-Path -LiteralPath $Chemins["cli"])) {
            Write-Error "Binaire ollama.exe introuvable : $($Chemins['cli'])"
            exit 1
        }

        $reglesCreees = @()
        Remove-NetFirewallRule -Name $NomRegleCli -ErrorAction SilentlyContinue
        New-NetFirewallRule -Name $NomRegleCli -DisplayName $NomRegleCli `
            -Direction Outbound -Program $Chemins["cli"] -Action Block -Profile Any `
            -Description "Bloque tout trafic sortant d'ollama.exe (Épisode 3)." -ErrorAction Stop | Out-Null
        $reglesCreees += $NomRegleCli

        if ($Chemins["app"] -and (Test-Path -LiteralPath $Chemins["app"])) {
            Remove-NetFirewallRule -Name $NomRegleApp -ErrorAction SilentlyContinue
            New-NetFirewallRule -Name $NomRegleApp -DisplayName $NomRegleApp `
                -Direction Outbound -Program $Chemins["app"] -Action Block -Profile Any `
                -Description "Bloque tout trafic sortant d'ollama app.exe (Épisode 3)." -ErrorAction Stop | Out-Null
            $reglesCreees += $NomRegleApp
        } else {
            Write-Warning "ollama app.exe introuvable : pas de règle pour l'application Desktop."
        }

        if ($Chemins["runner"] -and (Test-Path -LiteralPath $Chemins["runner"])) {
            Remove-NetFirewallRule -Name $NomRegleRunner -ErrorAction SilentlyContinue
            New-NetFirewallRule -Name $NomRegleRunner -DisplayName $NomRegleRunner `
                -Direction Outbound -Program $Chemins["runner"] -Action Block -Profile Any `
                -Description "Bloque tout trafic sortant du moteur d'inférence llama-server.exe d'Ollama (Épisode 3)." -ErrorAction Stop | Out-Null
            $reglesCreees += $NomRegleRunner
        } else {
            Write-Warning "lib\ollama\llama-server.exe introuvable (version d'Ollama sans moteur séparé ?) : pas de règle pour le moteur d'inférence."
        }

        Write-Output "=== Binaires bloqués ==="
        foreach ($cle in @("cli", "app", "runner")) { if ($Chemins[$cle]) { Write-Output "  $cle : $($Chemins[$cle])" } }
        Write-Output "=== Profil réseau actif ==="
        Get-NetConnectionProfile | Select-Object Name, NetworkCategory | Format-Table -AutoSize
        Write-Output "=== Profils pare-feu actifs ==="
        Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object Name, Enabled | Format-Table -AutoSize
        Write-Output "=== Règles effectives (ActiveStore) ==="
        Get-NetFirewallRule -PolicyStore ActiveStore -Name $reglesCreees | Select-Object Name, Enabled, Direction, Action, Profile | Format-Table -AutoSize
        Write-Output "CONSEQUENCE : plus aucun 'ollama pull' ni mise à jour tant que ces règles sont actives. Utilisez '-Action Debloquer' pour retirer uniquement ces règles."
    }

    "Debloquer" {
        if (-not (Test-EstAdmin)) {
            Write-Error "Action 'Debloquer' : relancez PowerShell en tant qu'Administrateur."
            exit 1
        }
        $supprimees = @()
        foreach ($nom in $NomsRegles) {
            if (Get-NetFirewallRule -Name $nom -ErrorAction SilentlyContinue) {
                Remove-NetFirewallRule -Name $nom -ErrorAction Stop
                $supprimees += $nom
            }
        }
        if ($supprimees.Count -gt 0) {
            Write-Output "Règles supprimées (uniquement celles créées par ce script) : $($supprimees -join ', ')."
        } else {
            Write-Output "Aucune règle créée par ce script n'était présente."
        }
    }

    "Statut" {
        Write-Output "=== Binaires Ollama détectés ==="
        Write-Output "CLI/Serveur : $($Chemins['cli'])"
        if ($Chemins["app"]) { Write-Output "Desktop App : $($Chemins['app'])" }
        if ($Chemins["runner"]) { Write-Output "Moteur d'inférence : $($Chemins['runner'])" }
        Write-Output "`n=== Variables session Env:OLLAMA* ==="
        $vars = Get-ChildItem Env:OLLAMA* -ErrorAction SilentlyContinue
        if ($vars) { $vars | Format-Table Name, Value -AutoSize } else { Write-Output "(aucune variable OLLAMA_* définie dans cette session)" }
        Write-Output "=== Variables utilisateur Windows (persistantes) ==="
        $userVars = [Environment]::GetEnvironmentVariables("User").GetEnumerator() | Where-Object { $_.Key -like "OLLAMA*" } | Sort-Object Key
        if ($userVars) { $userVars | Format-Table Key, Value -AutoSize } else { Write-Output "(aucune variable OLLAMA_* persistante au niveau Utilisateur)" }
        if (Test-Path -LiteralPath $FichierSauvegardeEnv) {
            Write-Output "Sauvegarde EnvUtilisateur présente : $FichierSauvegardeEnv"
        }
        if ($env:OLLAMA_ORIGINS -and ($env:OLLAMA_ORIGINS.Split(',') | ForEach-Object { $_.Trim() }) -contains "*") {
            Write-Warning "OLLAMA_ORIGINS contient '*' : toute page web ouverte dans le navigateur peut interroger http://127.0.0.1:11434 !"
        }
        Write-Output "=== Règles pare-feu créées par ce script ==="
        $regles = @(Get-NetFirewallRule -Name $NomsRegles -ErrorAction SilentlyContinue)
        if ($regles) {
            $regles | Select-Object Name, Direction, Action, Enabled, Profile | Format-Table -AutoSize
            $absentes = $NomsRegles | Where-Object { $_ -notin $regles.Name }
            if ($absentes) { Write-Warning "Règle(s) manquante(s) : $($absentes -join ', ') (relancez -Action Bloquer)." }
        }
        else { Write-Output "(non installées : ollama.exe, ollama app.exe et llama-server.exe peuvent sortir sur Internet)" }
    }
}
