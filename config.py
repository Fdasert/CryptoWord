import os

BOT_TOKEN       = os.environ["BOT_TOKEN"]
SUPERADMIN_IDS  = [int(x) for x in os.environ.get("SUPERADMIN_IDS", "518544601").split(",") if x.strip()]
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
WEBHOOK_SECRET  = os.environ.get("WEBHOOK_SECRET", "")
DB_PATH = "ratings.db"
