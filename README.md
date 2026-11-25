# Local LLM Benchmarks on 16 GB RAM & 4 GB VRAM (Ollama)

Minimalist, **100% reproducible** benchmark repository accompanying Episode 2 of **“Local LLMs: What I Can Actually Do on My Work Laptop”** (`qwen2.5:3b`, `llama3.2:3b`, `mistral:7b` on an `Intel Core i7-10750H`, `16 GB RAM`, `NVIDIA GTX 1650 Ti 4 GB VRAM`).

Every prompt is stored as a standalone text file in `prompts/` and passed directly to the Python scripts in `fonctions/` with automatic local `PASS / PARTIAL / FAIL` verification.

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
