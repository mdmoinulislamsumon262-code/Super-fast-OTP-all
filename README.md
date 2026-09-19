# Super Fast OTP Shop — Render Deploy (single file)

## Files
- `main.py` — puro bot (main + shop + shop_admin + temp_mail_engine ek file e merged)
- `requirements.txt`
- `render.yaml` — Render blueprint (web service, free plan, disk mount /var/data)

## Render deploy
1. Ei folder ta ekta GitHub repo te push korun.
2. Render > New > Blueprint > repo select korun (render.yaml auto detect hobe).
3. Environment variables set korun:
   - `BOT_TOKEN` = BotFather token
   - `ADMIN_ID`  = apnar Telegram user id
   - `DATA_DIR`  = `/var/data` (already set in render.yaml)
4. Deploy. Health check: `/health`

## Local run
```
pip install -r requirements.txt
export BOT_TOKEN=xxxx ADMIN_ID=123456 DATA_DIR=./data
python main.py
```


## Shop → Get Code

Shop menu now has **Get Code** (My Cart / History / Notifications removed).

### Mail Code (Microsoft OAuth)
Each user signs in with their own Microsoft account. No password, refresh token
or access token is ever typed into the Telegram chat.

Azure app registration (Web platform) redirect URI:

```
https://<your-render-url>/oauth/microsoft/callback
```

Delegated permissions: `openid`, `profile`, `email`, `offline_access`,
`User.Read`, `Mail.Read`.

Environment variables:
- `MS_CLIENT_ID`     — Azure application (client) ID
- `MS_CLIENT_SECRET` — Azure client secret value
- `MS_TENANT`        — `common` (default) or your tenant ID
- `MS_REDIRECT_URI`  — the callback URL above (falls back to `RENDER_EXTERNAL_URL` + `/oauth/microsoft/callback`)

Tokens are stored server-side only, encrypted at rest in `shop_ms_accounts`.
Expired/revoked authorization is detected and the user is asked to sign in again.

### 2FA Code (TOTP)
The user sends their own base32 setup key or an `otpauth://` link. The bot
returns the current code with its remaining validity. The key is kept in memory
for 5 minutes (refresh button) and is never written to the database.
