# LUX QR SYSTEM — Permanent QR + Monthly Excel History

This package fixes **only the QR public-access architecture**. The existing permanent QR IDs, Excel upload, current output, database mapping, QR page, and monthly history are preserved.

## Required QR flow

```text
Permanent QR
    ↓
Public HTTPS URL
    ↓
/scan/LUX-QR-00001
    ↓
Permanent QR ID lookup in database
    ↓
Current checking-location data + monthly history
```

A new Excel upload does **not** create a new QR ID for an existing checking location.

## Public QR URL configuration

The application uses `PUBLIC_BASE_URL` when it is set:

```text
PUBLIC_BASE_URL=https://your-public-domain.com
```

QR generation uses:

```python
scan_url = f"{PUBLIC_BASE_URL.rstrip('/')}/scan/{qr_id}"
```

On Render, if `PUBLIC_BASE_URL` is empty, the application automatically uses Render's `RENDER_EXTERNAL_URL`, so a production QR will not silently use `192.168.x.x`, `localhost`, or `127.0.0.1`.

For a custom domain, set `PUBLIC_BASE_URL` to the HTTPS origin only. Do not add `/scan/...`.

## Important: a public server is required

A local address such as `http://192.168.0.132:5000` can only be reached from an appropriate local network. For worldwide scanning, deploy this Flask application to a public HTTPS host.

## Render deployment

### Option A — Render Blueprint (recommended)

The ZIP includes `render.yaml` with:

- Python web service
- Gunicorn
- HTTPS public service URL
- `/health` health check
- one service instance
- persistent disk mounted at `/var/data`
- `LUX_QR_DATA_DIR=/var/data`
- generated `SECRET_KEY`
- optional `PUBLIC_BASE_URL`

1. Put the complete project folder into a GitHub repository.
2. In Render, create a new Blueprint and select that repository.
3. Render reads `render.yaml`.
4. Deploy the service.
5. After the first deploy, note the HTTPS service URL, for example:
   `https://lux-qr-system.onrender.com`
6. If you are using the Render URL, you may leave `PUBLIC_BASE_URL` empty because the app detects `RENDER_EXTERNAL_URL` automatically.
7. If you use a custom domain, set:
   `PUBLIC_BASE_URL=https://your-domain.com`
   and redeploy.

Render documentation:
- https://render.com/docs/blueprint-spec
- https://render.com/docs/disks
- https://render.com/docs/environment-variables

### Option B — Manual Render Web Service

Use:

**Build Command**

```text
pip install -r requirements.txt
```

**Start Command**

```text
gunicorn --bind 0.0.0.0:$PORT --workers 1 --access-logfile - app:app
```

Environment variables:

```text
LUX_QR_DATA_DIR=/var/data
SECRET_KEY=<generate-a-strong-secret>
PUBLIC_BASE_URL=https://your-public-domain.com
```

If you use the default Render domain, `PUBLIC_BASE_URL` can be omitted and `RENDER_EXTERNAL_URL` is used automatically.

Attach a persistent disk at:

```text
/var/data
```

Keep the service at **one instance** because this project uses SQLite. Do not remove the supplied `lux_qr.db` from the initial deployment package.

## Why the persistent disk matters

The permanent QR mapping and monthly history are stored in SQLite. The application copies the supplied `lux_qr.db` to `/var/data/lux_qr.db` on first startup when the persistent database does not yet exist, and never overwrites an existing persistent database.

This preserves the current permanent QR IDs and history across normal service restarts/deploys.

## Verify deployment

First open:

```text
https://YOUR-PUBLIC-DOMAIN/health
```

Expected:

```json
{"status":"ok"}
```

Then test:

```text
https://YOUR-PUBLIC-DOMAIN/scan/LUX-QR-00001
```

It should show the checking-location data for `LUX-QR-00001`.

You can also test:

```text
https://YOUR-PUBLIC-DOMAIN/api/scan/LUX-QR-00001
```

which returns the current location record and its monthly history as JSON.

## QR regeneration / any-mobile scanning

After deployment, open the public **/qr_codes** page once. The system automatically rebuilds every permanent QR PNG using the actual public HTTPS hostname seen by the browser. You can also click **Regenerate QR for This Public URL**.

The QR contains a normal HTTPS web address such as:

```text
https://your-app.onrender.com/scan/LUX-QR-00001
```

Therefore any mobile phone that can access the internet can scan it and open the current LUX data. The phone does **not** need to be connected to the laptop's Wi-Fi.

If the public domain changes, open **/qr_codes** again and regenerate/print the QR sheets again.

## QR regeneration

When a public HTTPS origin is configured, the application regenerates the existing QR PNG files using the same permanent IDs. For example:

```text
LUX-QR-00003
```

will encode:

```text
https://YOUR-PUBLIC-DOMAIN/scan/LUX-QR-00003
```

The QR ID does not change. Only the URL embedded in the PNG changes from the old local/private address to the public HTTPS address.

## Monthly upload behavior

The existing workflow is preserved:

```text
January Excel
    ↓
LUX-QR-00001 → January current data

February Excel
    ↓
LUX-QR-00001 → February/current data
             ↓
          January remains in history
```

Do not delete `lux_qr.db`.

## Local development

PowerShell:

```powershell
cd "D:\Maharajan\Desktop\LUX_QR_SYSTEM"
python -m pip install -r requirements.txt
python app.py
```

For a local-only test, the app can use the current local host for newly generated QR codes. Those local QR codes are not worldwide-accessible.

For production/public QR generation, set:

```powershell
$env:PUBLIC_BASE_URL="https://your-public-domain.com"
python app.py
```

## Existing routes

- `/` → admin
- `/admin` → admin page
- `/qr_codes` → QR listing/printing page
- `/scan/<qr_id>` → public scan page
- `/api/scan/<qr_id>` → public scan API
- `/history` → monthly history
- `/health` → deployment health check

## Production rule

Never embed these in production QR codes:

```text
192.168.x.x
localhost
127.0.0.1
```

Production QR codes must contain a public HTTPS origin.

## Exact required behavior

This package implements **permanent QR + monthly Excel + previous-month history + current data + any-mobile scanning**.

- Each location keeps one permanent QR ID, for example `LUX-QR-00001`.
- The QR encodes only a public HTTPS `/scan/<QR_ID>` address.
- Uploading a new Excel updates the current location data and appends that month's snapshot to the `history` table.
- The printed QR does not change when the month changes.
- Scanning the same QR always opens the latest/current data and shows the saved monthly history.
- Any mobile with internet access can scan it once the Flask application is deployed to a public HTTPS address.
- `PUBLIC_BASE_URL` is optional on Render: if it is empty, the application automatically uses Render's `RENDER_EXTERNAL_URL`.

### Deployment and printing

1. Deploy this project using the supplied `render.yaml`.
2. Wait for the public HTTPS service URL to become live.
3. Open the public `/qr_codes` page once. Existing QR PNGs are regenerated using the public HTTPS URL while the permanent QR IDs stay unchanged.
4. Scan one QR using a phone on **mobile data with Wi-Fi turned off**. This confirms that it is truly accessible from outside the laptop's LAN.
5. Only after that test succeeds, print the QR codes.
6. Each month, upload the new Excel. Do not delete `lux_qr.db` or the Render persistent disk.

### What can and cannot be done

A permanently printed QR cannot fetch changing monthly data from a completely offline laptop. The QR is a permanent pointer; the changing data must live in an internet-accessible database/application. Therefore a public deployment is required for the phrase **"scan by anyone mobile anywhere"**.
