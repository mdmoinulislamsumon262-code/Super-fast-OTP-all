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
