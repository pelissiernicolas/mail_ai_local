# mail_ai_local

Outil local d'analyse d'e-mails (privacy-first).

Fonctionnalités
- Ingestion d'un MBOX (Gmail Takeout) vers SQLite
- Statistiques rapides (top senders, timeline, taille, pièces jointes)
- Résumés et propositions d'étiquettes via un LLM local (Ollama)

Usage rapide
```powershell
python mail_ai_local.py ingest --mbox "C:\path\to\All mail Including Spam and Trash.mbox" --db ./mail.db
python mail_ai_local.py stats --db ./mail.db
python mail_ai_local.py top-senders --db ./mail.db --limit 30
python mail_ai_local.py summarize --db ./mail.db --model mistral --limit 200
```

Remarques
- Le script est conçu pour être utilisé localement (aucune donnée envoyée par défaut sauf si Ollama est configuré localement).
- Exclure `mail.db` et les fichiers MBOX du dépôt (voir `.gitignore`).
