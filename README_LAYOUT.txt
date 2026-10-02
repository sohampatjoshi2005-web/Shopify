Unzip INTO your repo root. Then add your ORIGINAL backend folder `app/` (db.py, main.py, models.py, models_market.py,
schemas.py, security.py, notify.py, services/, config.py, ...) so that app/ext/ sits inside it.
Do NOT overwrite app/__init__.py. In app/main.py add (after app = FastAPI(...), before create_all):
    from .ext import install; install(app)
Delete the old flat files and the "(1)" / "(17)" duplicates from the repo root. Merge requirements.txt with your original one.
