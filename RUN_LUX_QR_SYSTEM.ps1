# LOCAL DEVELOPMENT ONLY
# Leave PUBLIC_BASE_URL empty to use the local browser/LAN URL for local testing.
# For production QR codes, set PUBLIC_BASE_URL to your public HTTPS URL.

$env:PUBLIC_BASE_URL=""

python app.py
