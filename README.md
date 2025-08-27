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

# IA Gmail — Outils locaux

Un ensemble de scripts Python pour analyser, classer et nettoyer une boîte Gmail localement.

Principaux scripts
------------------
- `mail_ai_local.py`: ingestion MBOX -> SQLite, résumé automatique et extraction d'étiquettes via LLM.
- `ai_decider.py`: appelle l'IA pour décider `keep|archive|delete`, stocke la décision dans `mail.db` et fournit des règles d'override utilisateur.
- `apply_gmail_labels.py`: synchronise les décisions/étiquettes sur Gmail (DRY-RUN par défaut).
- `delete_ai.py`: supprime en masse les messages marqués `_AI_DELETE` (ATTENTION: irréversible).
- `decide_cleanup.py`: utilitaire de prévisualisation et règles manuelles.

Prérequis
---------
- Python 3.10+ (venv recommandé).
- `credentials.json` (OAuth Desktop) dans le dossier pour l'accès Gmail.
- Un LLM local (ex: Ollama) pour utiliser les fonctions de résumé (optionnel).

Installation rapide (Windows PowerShell)
--------------------------------------
```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configuration Gmail
-------------------
1. Créez un projet Google Cloud, activez l'API Gmail et créez des identifiants OAuth (type "Desktop").
2. Placez `credentials.json` dans ce dossier.
3. Au premier run un navigateur s'ouvrira pour autoriser l'accès; `token.json` sera créé.

Usage courant
-------------
- Ingestion MBOX -> DB:
```
python mail_ai_local.py ingest --mbox All-mail.mbox --db .\mail.db
```
- Synthèse / génération d'étiquettes (dry-run recommandé):
```
python mail_ai_local.py summarize --db .\mail.db --limit 200 --dry
```
- Décisions IA (keep/archive/delete):
```
python ai_decider.py decide --db .\mail.db --model mistral --limit 500 --num-predict 160 --temp 0.1
```
- Appliquer labels sur Gmail (DRY-RUN par défaut):
```
python apply_gmail_labels.py --db .\mail.db --limit 200 --log-csv actions.csv --verbose
```
Ajoutez `--no-dry-run` pour exécuter réellement les actions.

- Supprimer les messages marqués `_AI_DELETE` (ATTENTION):
```
del .\token.json  # forcer nouvelle authent
python delete_ai.py
```

Bonnes pratiques & précautions
------------------------------
- Testez d'abord en dry-run et/ou sur un compte test.
- `delete_ai.py` supprime définitivement — utilisez-le avec extrême prudence.
- Si vous obtenez `Insufficient Permission`, supprimez `token.json` et relancez pour forcer la ré-authentification avec les scopes requis.

Dépannage rapide
----------------
- `ModuleNotFoundError: No module named 'google'`: activez le venv et installez `pip install -r requirements.txt`.
- JSON tronqué depuis le LLM: le code inclut des fonctions de "salvage" pour récupérer partiellement le JSON.

Contribuer
----------
PRs bienvenus. Commencez par des changements petits et testables (p. ex. tests unitaires pour helpers).

Licence
-------
MIT

## Commandes utiles (copier/coller)

Quelques commandes prêtes à l'emploi pour les opérations courantes (PowerShell).

- Créer et activer l'environnement virtuel :

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

- Ingest (mbox -> sqlite) :

```powershell
python .\mail_ai_local.py ingest --mbox All-mail.mbox --db .\mail.db
```

- Résumer / générer étiquettes (dry-run) :

```powershell
python .\mail_ai_local.py summarize --db .\mail.db --limit 200 --dry
```

- Lancer l'IA pour décisions (exemple) :

```powershell
python .\ai_decider.py decide --db .\mail.db --model mistral --limit 500 --num-predict 160 --temp 0.1
```

- Appliquer labels sur Gmail (dry-run, journal CSV) :

```powershell
python .\apply_gmail_labels.py --db .\mail.db --limit 200 --log-csv actions.csv --verbose
```

- Appliquer labels réellement (supprime le mode dry-run) :

```powershell
python .\apply_gmail_labels.py --db .\mail.db --limit 200 --log-csv actions.csv --verbose --no-dry-run
```

- Rejouer les overrides utilisateur sur la DB :

```powershell
python .\ai_decider.py overrides --db .\mail.db
```

- Export CSV des décisions AI :

```powershell
python .\ai_decider.py export --db .\mail.db --out .\decisions_ai.csv
```

- Supprimer définitivement les messages marqués `_AI_DELETE` (vérifier token & scopes):

```powershell
del .\token.json  # si nécessaire pour forcer ré-auth
python .\delete_ai.py
```

- Commande d'inspection rapide (compter résumés/étiquetés) :

```powershell
python .\mail_ai_local.py count --db .\mail.db --what summarized
python .\mail_ai_local.py count --db .\mail.db --what labeled
```

Gardez ces commandes comme référence. Faites d'abord des dry-runs et validez les résultats (CSV, logs) avant d'exécuter des opérations irréversibles.
