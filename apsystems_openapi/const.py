DOMAIN = "apsystems_openapi"
DEFAULT_BASE_URL = "https://api.apsystemsema.com:9282"
PLATFORMS = ["sensor", "button"]

# Default to 60 minute intervals to stay under 1000 queries/month
DEFAULT_SCAN_INTERVAL = 3600  # seconds (hourly energy)

# Summary (lifetime/today/month/year) changes slowly and overlaps with
# the hourly series, so fetch it less often to save API budget.
DEFAULT_SUMMARY_SCAN_INTERVAL = 14400  # seconds (4 hours)

# Inverter data is fetched less frequently to conserve API budget.
# 28800s = 8 hours → ~1.4 fetches/day during solar hours.
DEFAULT_INVERTER_SCAN_INTERVAL = 28800  # seconds (8 hours)

# Optional "focus inverter" gets polled more frequently (e.g. every hour)
# to give near-real-time data for one inverter without blowing the budget.
DEFAULT_FOCUS_INVERTER_SCAN_INTERVAL = 3600  # seconds (1 hour)

# Monthly budget estimate (11 solar hours/day, 30 days, 6 inverters):
#   Hourly energy:       11h × 1/h × 30                  = 330
#   Summary:             11h ÷ 4h × 30                   =  83
#   Inverter energy:     11h ÷ 8h × 6inv × 30            = 248
#   Focus inverter:      11h ÷ 1h × 1inv × 30            = 330
#   Inverter list:       manual button only               =   0
#   Total (with focus)                                    ≈ 991 / 1000
#   Total (no focus)                                      ≈ 661 / 1000