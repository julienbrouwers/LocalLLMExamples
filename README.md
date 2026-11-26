# Local LLM Benchmarks & Privacy Lockdown on 16 GB RAM & 4 GB VRAM (Ollama)

Minimalist, **100% reproducible** repository accompanying **“Local LLMs: What I Can Actually Do on My Work Laptop”** (`qwen2.5:3b`, `llama3.2:3b`, `mistral:7b` on an `Intel Core i7-10750H`, `16 GB RAM`, `NVIDIA GTX 1650 Ti 4 GB VRAM`).

- **Branch `episode-2-llmlocaux`** (tag `v2-episode-2`): Episode 2 benchmarks (legacy PHP audit, PDF parser codegen, CSV vs JSON search, categorization, grounded recommendations).
- **Branch `episode-3-llmlocaux`** *(this branch)*: adds Episode 3 **Privacy & Offline Lockdown** (automated network/isolation audit, PowerShell hardening & reversible Windows Defender Firewall block, zero-network Docker airgap container).

---

## Episode 3 — Reproducing Privacy & Offline Lockdown Step by Step

### Prerequisites & Baseline Capture (From a Fresh Clone)

```powershell
git clone -b episode-3-llmlocaux https://github.com/julienbrouwers/LocalLLMExamples.git
Set-Location .\LocalLLMExamples

# 1. Record exact runtime versions (also check Docker Desktop -> About for its UI version)
ollama --version
docker version
docker compose version

# 2. Preload qwen2.5:3b and record its exact API version & digest
ollama pull qwen2.5:3b
$api = 'http://127.0.0.1:11434'
Invoke-RestMethod "$api/api/version"
(Invoke-RestMethod "$api/api/tags").models | Where-Object name -eq 'qwen2.5:3b' | Select-Object name, digest

# 3. Inspect listening socket and exact process paths
Get-NetTCPConnection -State Listen -LocalPort 11434 | Select-Object LocalAddress, LocalPort, OwningProcess
Get-Process -Name 'ollama','ollama app' -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, Path
```

### Step 1 — Baseline Audit & Cold vs. Warm Canary Run (No Admin Required)

`fonctions/verifier_isolation_ollama.py` generates a **fresh unique canary marker** per run (`CANARI-CONFIDENTIEL-LOCAL-<uuid>`), reports each check individually with an explicit state (`reussi`, `echec`, `non_execute`, `non_concluant`), and logs versions, model digest, `total_duration`, `load_duration`, `ollama ps` `PROCESSOR`, TCP sampling window, inspected files, and capture limits:

```powershell
# Cold run (force unload first, then keep_alive=5m to allow a warm follow-up)
ollama stop qwen2.5:3b
python -X utf8 .\fonctions\verifier_isolation_ollama.py --modele qwen2.5:3b --keep-alive 5m

# Warm run (within 5 minutes) + full JSON report
python -X utf8 .\fonctions\verifier_isolation_ollama.py --modele qwen2.5:3b --keep-alive 5m --json
```

Expected out-of-the-box states: `binding_loopback=reussi`, `echo_canari_exact=reussi`, `zero_connexion_distante=reussi`, `zero_requete_dns_canari=reussi`, `zero_canari_dans_server_log=reussi` (or `non_concluant` if `server.log` is absent), `variables_durcissement=echec`, `pare_feu_sortant_bloque=echec`, `capture_paquets_pktmon=non_execute` -> global verdict `PARTIAL`.

*(Optional witness vs. canary packet capture in a second Administrator PowerShell, zero third-party driver install:)*

```powershell
# Replay once with $run = 'temoin' (idle witness), then once with $run = 'canari'
$run = 'canari'
pktmon comp list
pktmon start --capture --pkt-size 128 --file-name ".\$run.etl"
# Wait during 'temoin', or run verifier_isolation_ollama.py in the other terminal during 'canari'
pktmon stop
pktmon etl2pcap ".\$run.etl" --out ".\$run.pcapng"
# Inspect in Wireshark, then clean up: Remove-Item .\temoin.*, .\canari.* -ErrorAction SilentlyContinue
```

### Step 2 — Tier 1: Local Server Hardening (No Admin Required)

Ollama only reads `OLLAMA_*` variables **at server startup**. Quit Ollama Desktop first, then launch `ollama serve` from a hardened session (or persist User env vars with automatic backup/restore):

```powershell
# Option A: session-only hardening, then start `ollama serve` in this shell
. .\securiser-ollama-windows.ps1 -Action EnvSession
ollama serve

# Option B: persist to Windows User env (saves exact previous values to %TEMP%\ollama-ep3-env-backup.json), then restart Ollama
.\securiser-ollama-windows.ps1 -Action EnvUtilisateur

# Rollback (restores exact previous User env values from backup; if no backup exists, clears session vars only and preserves pre-existing User vars)
. .\securiser-ollama-windows.ps1 -Action Reinitialiser
```

> **Security notes:** Ollama has **no built-in authentication** (never bind `0.0.0.0` or Docker `-p 11434:11434` without an authenticated reverse proxy or VPN) and never include `*` in `OLLAMA_ORIGINS` (flagged as `echec` by `verifier_isolation_ollama.py`).

### Step 3 — Tier 2: Windows Defender Outbound Firewall (Admin, Reversible)

Before creating rules, verify that the registry is reachable (`ollama pull tinyllama` succeeds; otherwise a negative block test is inconclusive). Then block **both** `ollama.exe` and `ollama app.exe`, verify pull failure + local inference success, and roll back cleanly (removes **only** the two rules created by this script):

```powershell
# 1. Connected witness (must succeed before blocking)
ollama pull tinyllama

# 2. Block both ollama.exe and ollama app.exe (prints active profiles & ActiveStore rules)
.\securiser-ollama-windows.ps1 -Action Bloquer

# 3. Verify remote pull fails while local canary inference passes --strict (exit 0)
ollama pull tinyllama
"Pull exit code: $LASTEXITCODE"
. .\securiser-ollama-windows.ps1 -Action EnvSession
python -X utf8 .\fonctions\verifier_isolation_ollama.py --modele qwen2.5:3b --strict

# 4. Rollback: removes ONLY Ollama-Block-Outbound-LocalOnly and Ollama-Block-Outbound-App-LocalOnly
.\securiser-ollama-windows.ps1 -Action Debloquer
```

> **Side effects:** while blocked, `ollama pull`, cloud features, and automatic updates fail. Schedule controlled maintenance windows (`Debloquer` -> update/pull -> `Bloquer`).

### Step 4 — Tier 3: Zero-Network Docker Airgap (Digest-Pinned + Models-Only `:ro`)

Pin the exact image digest and bind-mount **only** your local models directory read-only to `/models` (no port published):

```powershell
# 1. Pull the exact tested digest directly and resolve the host models directory
$env:EP3_OLLAMA_IMAGE = 'ollama/ollama@sha256:d00473cb58f0082c07cd6ed0d326a8a86f443ab69c51f8fc2b1a41687d45c661'
docker pull $env:EP3_OLLAMA_IMAGE
$env:EP3_MODELS       = (Resolve-Path -LiteralPath "$env:USERPROFILE\.ollama\models").Path

# (Optional: to pin a newer image instead, run `docker pull ollama/ollama:latest`
#  and `$env:EP3_OLLAMA_IMAGE = docker image inspect ollama/ollama:latest --format '{{index .RepoDigests 0}}'`)

# 2. Validate effective Compose config and start offline (--pull never)
$compose = @('-p', 'ollama-ep3', '-f', '.\docker-compose.offline.yml', '--profile', 'airgap')
docker compose @compose config
docker compose @compose up --pull never -d

# 3. Verify NetworkMode=none, read-only /models mount, lo-only interface, local model visibility, pull failure, and local inference
$cid = docker compose @compose ps -q ollama-airgap
docker inspect $cid --format '{{.HostConfig.NetworkMode}} {{json .Mounts}}'
docker compose @compose exec -T ollama-airgap cat /proc/net/dev
docker compose @compose exec -T ollama-airgap ollama list
docker compose @compose exec -T --interactive=false ollama-airgap ollama pull tinyllama
"Pull exit code: $LASTEXITCODE"
Measure-Command { docker compose @compose exec -T --interactive=false ollama-airgap ollama run qwen2.5:3b 'Reply with OK only.' | Out-Host } | Select-Object TotalSeconds
docker compose @compose exec -T ollama-airgap ollama ps

# 4. Rollback / clean up
docker compose @compose down
```

**Optional `loopback` profile (separate behavior):** `docker-compose.offline.yml` also includes `--profile loopback`, which publishes `127.0.0.1:11435:11434` (restricting **inbound** LAN access only) while keeping Docker's default bridge **outbound** Internet access. Only `--profile airgap` (`network_mode: none`) cuts outbound traffic at the kernel level.

### Step 5 — Disk & Log Hygiene Audit + Zero-Trace Usage

```powershell
# Inspect plaintext local artifacts
Get-ChildItem "$env:LOCALAPPDATA\Ollama" | Select-Object Name, Length, LastWriteTime

# Interactive terminal chat with no CLI history and instant VRAM unload
$env:OLLAMA_NOHISTORY = "1"
ollama run qwen2.5:3b --keepalive 0
```

If you used the Ollama Desktop Chat UI, quit Ollama from the system tray and purge `%LOCALAPPDATA%\Ollama\db.sqlite*` and `server*.log` (on a BitLocker-encrypted SSD).

---

## 1. Repository Layout (Strict Minimum)

```text
LocalLLMExamples/
├── donnees/
│   ├── entrees/
│   │   ├── calculs_legacy.php                     # 5 legacy PHP functions (Part 1)
│   │   ├── factures/FA000101.pdf..FA000104.pdf    # 4 anonymized PDF invoices, 8 purchases (Part 2)
│   │   ├── champs-facture.txt                     # Witness invoice JSON (Part 2)
│   │   └── livres-openlibrary.json                # Cached 92 real books from OpenLibrary (Part 2C)
│   └── inventaires/
│       ├── inventaire-qwen-verification.xlsx      # Consolidated Excel inventory (8 PDF purchases)
│       └── inventaire-100-vrais-livres-categorise.csv  # 100 categorized real books
├── fonctions/
│   ├── commun.py                                  # Shared Ollama HTTP client & CSV/Excel helpers
│   ├── verifier_isolation_ollama.py               # Episode 3: 7-check local isolation & canary data-leak verifier
│   ├── tester_php_unitaire.py                     # Part 1: Stateless PHP audit + dynamic JSON Schema + verifier
│   ├── generer_parseur_pdf.py                     # Part 2A: LLM Python parser codegen + live test on 4 PDFs
│   ├── import_factures_qwen.py                    # Part 2A: Full deterministic PDF -> Excel pipeline
│   ├── lire_facture.py                            # Part 2A: Direct single-PDF JSON extraction
│   ├── interroger_comics.py                       # Part 2B: CSV vs JSON input benchmark & 1,000-book chunks
│   ├── categoriser_livres.py                      # Part 2C: Genre categorization (64-row stateless chunks)
│   └── recommander_livres.py                      # Part 2C: Grounded reading recommendations (1 ms pre-filter)
├── prompts/
│   ├── php-{qwen,llama,mistral}-{fr,eng}.txt      # Part 1 generalist PHP audit prompts (FR / EN)
│   ├── pdf-codegen-{qwen,llama,mistral}-{fr,eng}.txt  # Part 2A PDF parser codegen prompts (FR / EN)
│   └── recherche-inventaire-{fr,eng}.txt          # Part 2B CSV vs JSON search prompts (FR / EN)
├── securiser-ollama-windows.ps1                   # Episode 3: PowerShell hardening & reversible firewall block
├── docker-compose.offline.yml                     # Episode 3: Zero-network Docker airgap & loopback profiles
├── rejouer-essais-ollama.ps1                      # PowerShell runner for shared vs isolated chat tests
├── requirements.txt                               # Python dependencies
├── Dockerfile / docker-compose.yml                # Optional containerized execution
└── README.md
```

---

## 2. Quick Setup

```powershell
# 1. Pull the 3 local models in Ollama
ollama --version
ollama pull qwen2.5:3b
ollama pull llama3.2:3b
ollama pull mistral:7b
ollama list

# 2. Install Python dependencies
python -m pip install -r requirements.txt
```

---

## 3. Reproducible Commands (In Article Order)

### Part 1 — Legacy PHP Audit & Deterministic Unit Tests (`tester_php_unitaire.py`)

1. **Inspect Modelfiles (`ChatML`, `Meta headers`, `[INST]`):**
   ```powershell
   foreach ($m in 'qwen2.5:3b', 'llama3.2:3b', 'mistral:7b') { ollama show $m --parameters --template }
   ```
2. **Reproduce V1 failures (shared history `13,722` tok vs isolated `2,190` tok, and mental math):**
   ```powershell
   .\rejouer-essais-ollama.ps1 -Essai PHP -FichierPHP .\donnees\entrees\calculs_legacy.php -Langues FR -Modeles llama3.2:3b -ModeConversation Partage -Contexte 8192
   .\rejouer-essais-ollama.ps1 -Essai PHP -FichierPHP .\donnees\entrees\calculs_legacy.php -Langues FR -Modeles llama3.2:3b -ModeConversation Separe -Contexte 8192
   python -X utf8 .\fonctions\tester_php_unitaire.py --modele llama3.2:3b --ancien-prompt --mesures
   ```
3. **Run V3 external generalist prompts (FR vs EN · `6/6 PASS`, `-11.6%` to `-13.2%` input tokens in EN):**
   ```powershell
   # Explicit prompt file
   python -X utf8 .\fonctions\tester_php_unitaire.py --fichier .\donnees\entrees\calculs_legacy.php --fonction calculTva --prompt .\prompts\php-qwen-eng.txt --modele qwen2.5:3b --mesures

   # All 3 models in FR and EN (auto-loads prompts/php-*-{fr|eng}.txt)
   foreach ($m in 'qwen2.5:3b', 'llama3.2:3b', 'mistral:7b') {
       python -X utf8 .\fonctions\tester_php_unitaire.py --fonction calculTva --modele $m --langue fr --mesures
       python -X utf8 .\fonctions\tester_php_unitaire.py --fonction calculTva --modele $m --langue eng --mesures
   }
   ```

---

### Part 2A — PDF Invoices to Excel: LLM Parser Code Generation (`generer_parseur_pdf.py`)

Each LLM generates `parse_articles(lines)` from a `pymupdf` excerpt of `FA000104.pdf`, then Python compiles and tests it live against all 4 real PDFs (`8/8` expected articles + `ValueError` guard):

```powershell
# 1. Benchmark all 3 models (EN: 3/3 PASS | FR: 1/3 PASS)
foreach ($m in 'qwen2.5:3b', 'llama3.2:3b', 'mistral:7b') {
    python -X utf8 .\fonctions\generer_parseur_pdf.py --modele $m --langue eng --mesures
}

# 2. Repeatability test across seeds & temperature (Qwen 3/3, Llama 3/3, Mistral 2/3)
foreach ($cfg in @( @{t=0.0;s=42}, @{t=0.2;s=101}, @{t=0.2;s=2026} )) {
    python -X utf8 .\fonctions\generer_parseur_pdf.py --modele qwen2.5:3b --langue eng --temperature $cfg.t --seed $cfg.s --mesures
}

# 3. Export the consolidated Excel inventory
python -X utf8 .\fonctions\generer_parseur_pdf.py --modele qwen2.5:3b --langue eng --sortie-excel .\donnees\inventaires\inventaire-qwen-verification.xlsx
```

---

### Part 2B — Inventory Search: CSV Text vs. JSON & 1,000-Book Chunking (`interroger_comics.py`)

```powershell
# 1. Benchmark CSV text vs. JSON input format (+24% JSON token overhead)
foreach ($m in 'qwen2.5:3b', 'llama3.2:3b', 'mistral:7b') {
    foreach ($fmt in 'csv', 'json') {
        "alien" | python -X utf8 .\fonctions\interroger_comics.py --modele $m --format-entree $fmt --langue eng --mesures
    }
}

# 2. Scale to 1,000 books in 16 stateless chunks of 64 rows at 100% VRAM (num_ctx: 4096)
"alien" | python -X utf8 .\fonctions\interroger_comics.py --modele llama3.2:3b --contexte 4096 --lignes-fictives 1000 --max-lignes 1000 --taille-bloc 64 --json --mesures
```

---

### Part 2C — Genre Categorization & Grounded Recommendations

```powershell
# 1. Categorize 8 PDF purchases, then 100 real books in 2 chunks (64 + 36 rows)
python -X utf8 .\fonctions\categoriser_livres.py --modele qwen2.5:3b --regle-accessoire --mesures
python -X utf8 .\fonctions\categoriser_livres.py --modele qwen2.5:3b --livres-api 92 --max-lignes 100 --taille-bloc 64 --regle-accessoire --sortie-csv .\donnees\inventaires\inventaire-100-vrais-livres-categorise.csv --mesures

# 2. Grounded recommendations (100 real books) vs. ungrounded memory (--sans-catalogue)
"1" | python -X utf8 .\fonctions\recommander_livres.py --modele qwen2.5:3b --mesures
"1" | python -X utf8 .\fonctions\recommander_livres.py --modele qwen2.5:3b --seulement-pdf --sans-catalogue --mesures
```

---

## 4. Optional Docker Execution

```powershell
docker compose build
docker compose run --rm tests python -X utf8 fonctions/tester_php_unitaire.py --fonction calculTva --modele qwen2.5:3b --langue eng --mesures
docker compose run --rm tests python -X utf8 fonctions/generer_parseur_pdf.py --modele qwen2.5:3b --langue eng --mesures
```
