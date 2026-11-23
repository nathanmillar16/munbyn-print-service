# Little Rose Munbyn print service

A small LAN web page for submitting existing 4 x 6 inch PDF labels to the Ubuntu CUPS queue named `munbyn`. The service runs in Docker and submits jobs through the host CUPS Unix socket; the Ubuntu Munbyn driver remains responsible for rendering the PDF.

## Install on the Ubuntu printer server

1. Copy this folder to the server, for example as `~/munbyn-print-service`.
2. Confirm CUPS has the socket at `/run/cups/cups.sock` and the queue is named `munbyn`:

   ```bash
   lpstat -p munbyn
   ls -l /run/cups/cups.sock
   ```

3. Make a secret `.env` file:

   ```bash
   cd ~/munbyn-print-service
   cp .env.example .env
   nano .env
   ```

   Set `PRINT_WEB_TOKEN` to a random secret. Generate one with `openssl rand -hex 32` and paste it into `.env`.

4. Start the service:

   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

5. Open `http://<ubuntu-server-ip>:8088` from a computer on the home network. Paste a multiline customer address into the address box and choose **Fill address fields**, or enter the fields manually. Check the parsed fields, preview the label, and confirm printing. You can also upload an existing carrier PDF. The address form has editable Instagram, Facebook, and website details prefilled with Little Rose's current values; changes are remembered in that browser. The local webpage does not ask for the token. The service uses the queue's confirmed 4 x 6 option, `PageSize=w288h432`.

Keep port 8088 available only on the trusted home network. Do not forward it from the internet. If UFW is enabled, allow only your LAN subnet, replacing the example subnet with the one used at home:

```bash
sudo ufw allow from 192.168.0.0/24 to any port 8088 proto tcp
```

## API routes

The local address form previews through `POST /api/preview-address`, then submits to `POST /api/print-address` after the user confirms. The service generates the branded 4 x 6 PDF in memory and sends it to CUPS without retaining the generated file. The existing PDF upload submits to `POST /api/print-from-page`. These page endpoints are intended for the trusted home network and check that browser submissions come from the same origin. Address labels require ReportLab and the DejaVu fonts, which the Docker image installs during rebuild.

The page reads `GET /api/status` every five seconds to show whether the printer is idle, printing, or paused, along with queued jobs and up to five completed jobs retained by CUPS.

For server-to-server integration, `POST /api/print` accepts `multipart/form-data` with:

- `pdf`: a PDF label (maximum 20 MB)
- `copies`: integer from 1 to 10 (optional, defaults to 1)
- Header `Authorization: Bearer <PRINT_WEB_TOKEN>`

Successful response:

```json
{"status":"submitted","queue":"munbyn","job_id":"munbyn-42"}
```

The website should call this endpoint from a server-side admin action after checking the signed-in user's admin permission. Do not put the print token in browser code. A remote deployment will need a private, authenticated route from the website server to this home service (for example a VPN or an authenticated tunnel); do not expose this service directly to the public internet.

### Branded address labels and job feedback

For branded address labels, use the authenticated `POST /api/print-address-api` instead of generating the PDF in the website. It accepts JSON using the same field names as the local form:

```json
{
  "name": "Rebecca Millar",
  "address_line_1": "1 I Love You Lane",
  "address_line_2": "",
  "town": "Lovington",
  "county": "Loveshire",
  "postcode": "LO13 4OU",
  "instagram": "@little.rose_shop",
  "facebook": "@littlerosegifts",
  "website": "www.littlerose.shop",
  "copies": 1
}
```

`name`, `address_line_1`, `town`, and `postcode` are required; optional fields use the local form's defaults when omitted. `copies` is optional from 1 to 10. Send the same `Authorization: Bearer <PRINT_WEB_TOKEN>` header. The route uses the existing label template and returns the same submitted-job JSON response as `/api/print`.

The website can check `GET /api/print-jobs/{job_id}` with the same Bearer header. It returns `queued`, `printing`, `completed`, or `unknown`. `completed` means CUPS lists the job in its completed history; it does not verify that the physical label came out correctly. Show the immediate API response as “sent to printer/queued”, then poll this status route for a later CUPS update. Do not retry a timed-out print automatically because the original job may already have been accepted.

These server-to-server routes are `/api/print-address-api`, `/api/print-jobs/{job_id}`, and `/api/print`; the local browser routes `/api/print-address` and `/api/print-from-page` are not the website API. A CUPS `completed` status is only CUPS history and cannot confirm physical label output.
