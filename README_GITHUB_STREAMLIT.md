# Mise en ligne — dernière étape

Ce dossier est la version cloud raccordée à Supabase. Il ne contient ni base SQLite, ni profil de navigateur, ni secret.

1. Créez un dépôt **privé** sur GitHub.
2. Envoyez tout le contenu de ce dossier dans le dépôt.
3. Ouvrez Streamlit Community Cloud puis cliquez sur **Create app**.
4. Sélectionnez le dépôt et choisissez `app.py` comme fichier principal.
5. Dans **Advanced settings > Secrets**, saisissez :

```toml
USE_SUPABASE = true
DATABASE_URL = "VOTRE_ADRESSE_TRANSACTION_POOLER"
```

6. Cliquez sur **Deploy**.
7. Ouvrez aussi la page **Pilotage Equipe** dans le menu de l'application et vérifiez les données.

Important : la version cloud lit et modifie Supabase. La synchronisation automatique du CRM exécutée sur le Mac reste une étape séparée. Le bouton de synchronisation par navigateur a été retiré de la version Streamlit Cloud, car elle ne peut pas réutiliser la session CRM privée du Mac.

