# ChargeSpot – final candidate
Python 3.10+. Install `pip install -r requirements.txt`. Set ADMIN_PASSWORD and SECRET_KEY in Render environment (keep existing values). Start `gunicorn --workers 1 --threads 4 app:app`. For local development run `python app.py`. Database migrations are additive; back up before deployment. DATABASE_PATH and BACKUP_PATH can point to a persistent disk; ephemeral Render storage is not durable. HTTPS is supplied by the hosting platform.

Daily SQLite backup and admin download are included. To recover, stop the application, retain the current database, verify the backup with `sqlite3 backup.db 'PRAGMA integrity_check;'`, copy the backup to DATABASE_PATH, and restart. Never commit database files or secrets.

Payment and sensor events are educational simulations; notifications are in-app, not email/push. QR opens the booking page for authenticated check-in. The home page includes an illustrative map, browser geolocation or simulated Riyadh location, and distance sorting for the seeded station areas. Coordinates are illustrative; unknown areas have no distance. Driver UI is responsive web, not a native mobile application. Real payment, external notifications, native app, load testing, and fully separated REST/MVC layers are not completed. A 15-minute late grace period is used. No card data is collected.

Validation: Python syntax compilation only in the build environment (Flask installation unavailable). Run the acceptance checklist after deploying before claiming all requirements passed.
