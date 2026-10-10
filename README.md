# NYSE Floor Simulator

The simulator runs as a Streamlit app. Visitors can play as guests; signed-in users can save their career, settings, badges, and trades to Supabase and appear on the global leaderboard. It uses Supabase email/password authentication, not Google sign-in.

## Before publishing

1. **Rotate any exposed Supabase secret first.** A secret key that appeared in a screenshot or was otherwise shared should be considered compromised. In Supabase, open **Project Settings → API Keys**, revoke/rotate the exposed server secret, and use only its replacement. Do not paste the replacement into chat, source files, screenshots, or GitHub.
2. In the Supabase dashboard, open **SQL Editor**, create a query, paste the contents of [`supabase_schema.sql`](supabase_schema.sql), and run it. This creates the profile/trade tables and public leaderboard view.
3. In **Project Settings → API Keys**, copy the **Project URL**, a **Publishable key**, and the new **Secret key**. The publishable key is intended for client identification; the secret key is privileged and must stay server-side.
4. Configure Supabase **Authentication → Providers → Email** to allow email/password sign-up. Decide whether email confirmation is enabled; if enabled, configure outgoing email as needed.

## Run locally on Windows

Open PowerShell and run:

```powershell
cd "$HOME\OneDrive\Desktop\CLAUDE DAY TRADE"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
notepad .streamlit\secrets.toml
```

In Notepad, replace the example values with the values from Supabase and save:

```toml
[supabase]
url = "https://YOUR-PROJECT-REF.supabase.co"
anon_key = "YOUR-SB-PUBLISHABLE-KEY"
secret_key = "YOUR-NEW-SB-SECRET-KEY"
```

Then start the app:

```powershell
.\.venv\Scripts\streamlit.exe run streamlit_app.py
```

Open `http://localhost:8501`. Test guest play, create a test account, sign in, and confirm the profile/trade data appears in Supabase. When email confirmation is enabled, follow the confirmation link before signing in. Stop the local server with `Ctrl+C`.

## Publish using GitHub and Streamlit Community Cloud

1. Create a new GitHub repository. It can be private; make sure your Streamlit Community Cloud account is authorized to access it.
2. In PowerShell, from the project folder, initialize Git and check that the local secret file is ignored:

   ```powershell
   cd "$HOME\OneDrive\Desktop\CLAUDE DAY TRADE"
   git init -b main
   git status --short --ignored
   ```

   Confirm `.streamlit/secrets.toml` is listed as ignored, not staged. Do **not** add or push that file, `.venv`, or any secret key. Add only the project files, for example:

   ```powershell
   git add .gitignore README.md requirements.txt streamlit_app.py index.html supabase_schema.sql .streamlit/config.toml .streamlit/secrets.toml.example
   git status --short
   git commit -m "Publish Streamlit trading simulator"
   git remote add origin https://github.com/YOUR-GITHUB-NAME/YOUR-REPOSITORY.git
   git push -u origin main
   ```

   Replace the GitHub name and repository with your own. If Git was already initialized or a remote already exists, do not repeat the corresponding initialization/remote command.
3. Sign in at [Streamlit Community Cloud](https://share.streamlit.io/) with GitHub, choose **Create app**, select your repository and `main` branch, and set the main file to `streamlit_app.py`. Deploy the app.
4. In the deployed app's **Settings → Secrets**, paste the same three-key TOML configuration shown above, using the replacement secret. Save the settings to restart the app. Do not add the secret to GitHub.
5. In Supabase **Authentication → URL Configuration**, set the Site URL to the deployed `https://...streamlit.app` address and add that address to the allowed redirect URLs. Add `http://localhost:8501` as an allowed redirect URL when testing locally.
6. Open the deployed app, register and confirm a test account if required, sign in, and verify that its profile and trades persist and that the leaderboard loads. Guest mode remains available without an account.

## Notes

- Streamlit secrets belong in `.streamlit/secrets.toml` locally and in Streamlit Cloud's Secrets settings when deployed. `.gitignore` excludes the local secrets file.
- The app requires a Supabase **publishable** key (or legacy anon JWT) and a server-only **secret** key (or legacy service-role JWT). Never place the server key in browser code or publish it.
- The leaderboard shows public profile fields and refreshes periodically. Career equity is simulated, not a brokerage balance or investment result. Market data is for practice.
