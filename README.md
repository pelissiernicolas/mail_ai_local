## Utilisation

Compteurs

```powershell
python .\mail_ai_local.py count --db .\mail.db --what summarized
python .\mail_ai_local.py count --db .\mail.db --what labeled
python .\mail_ai_local.py count --db .\mail.db --what total
python .\mail_ai_local.py count --db .\mail.db --what attachments
python .\mail_ai_local.py count --db .\mail.db --what unprocessed
```

Export CSV compatible Excel (UTF-8 BOM)

```powershell
python .\mail_ai_local.py export-excel --db .\mail.db --out .\mails_utf8.csv
```

Résumés rapides + fichier JSONL au fil de l’eau

```powershell
python .\mail_ai_local.py summarize --db .\mail.db --model mistral \
  --limit 50 --clip 1200 --num-predict 80 --num-ctx 1536 --temp 0.1 \
  --out-jsonl summaries.jsonl
```
# IA locale pour analyser tes mails (Gmail)

Ce kit te laisse **tout faire en local** : ingestion du fichier Gmail Takeout (`.mbox`), stats rapides et, si tu veux, **résumé + auto‑étiquetage** des mails via un LLM local (Ollama).

## 0) Pré-requis
- Python 3.9+
- (Optionnel) [Ollama](https://ollama.com/) installé et lancé (`ollama serve`) avec un modèle local :  
  `ollama pull mistral`  ou  `ollama pull llama3.1`

## 1) Récupère tes mails
- Va sur Google Takeout → n’exporte que **Gmail** → format **MBOX**.
- Télécharge l’archive, décompresse-la. Le fichier MBOX ressemble à :  
  `Takeout/Mail/All mail Including Spam and Trash.mbox`

## 2) Crée un venv & installe les libs
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

## 3) Ingestion dans SQLite
```bash
python mail_ai_local.py ingest --mbox "All-maol.mbox" --db ./mail.db
```

## 4) Stats utiles
```bash
python mail_ai_local.py stats --db ./mail.db
python mail_ai_local.py top-senders --db ./mail.db --limit 30
python mail_ai_local.py timeline --db ./mail.db --by month   # ou --by year
python mail_ai_local.py export --db ./mail.db --out mails.csv
```

## 5) Résumé + labels (LLM local via Ollama)
- Lance Ollama et récupère un modèle :
```bash
ollama serve &
ollama pull mistral   # ou llama3.1 / qwen2.5
```
- Puis :
```bash
python mail_ai_local.py summarize --db ./mail.db --model mistral --limit 200
```
Le script :
- Résume chaque mail en 1–2 phrases
- Propose 1–3 labels (ex: Factures, Bancaire, Santé, Travail, Newsletter, Promotions, etc.)
- Enregistre dans SQLite (`summary`, `auto_labels`)

Tu peux relancer autant de fois que tu veux (il ne retraitera que ceux sans résumé).

## 6) Exploiter les résultats
- Ouvre `mails.csv` dans Excel/LibreOffice pour filtrer par `auto_labels` ou `size_bytes`.
- Ou branche un outil BI (Metabase, Datasette) sur `mail.db`.

## 7) Idées d’amélioration
- Ajouter un tableau de bord `streamlit` (graphiques, filtres).
- Embeddings locaux + recherche sémantique (FAISS/Chroma).
- Export automatique de **filtres Gmail** en XML à partir des labels proposés.

> Tout est **local** : ni contenu, ni métadonnées ne quittent ta machine.
