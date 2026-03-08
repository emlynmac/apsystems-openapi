DOMAIN = "apsystems_openapi"
DEFAULT_BASE_URL = "https://api.apsystemsema.com:9282"
PLATFORMS = ["sensor"]

# Default to 60 minute intervals to stay under 1000 queries/month
DEFAULT_SCAN_INTERVAL = 3600  # seconds

# Inverter data is fetched less frequently to conserve API budget.
# 14400s = 4 hours → ~2-3 fetches/day during solar hours.
# Each fetch makes 1 call per inverter, so total monthly inverter calls ≈
# N_inverters × fetches_per_day × 30.
DEFAULT_INVERTER_SCAN_INTERVAL = 14400  # seconds (4 hours)

# Inverter list rarely changes; cache for 24 hours before re-fetching.
INVERTER_LIST_CACHE_SECONDS = 86400