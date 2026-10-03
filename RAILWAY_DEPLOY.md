# Deploy the 3-hour test to Railway

## 1. Put 4 files in one folder
- `locustfile.py`
- `accounts.txt`
- `Dockerfile`
- (this file, optional)

## 2. Install the Railway CLI (on your computer, not the phone)
```
npm i -g @railway/cli
railway login
```
This opens a browser to log into your Railway account.

## 3. From that folder, create and deploy
```
railway init
railway up
```
`railway init` creates a new Railway project from the current folder.
`railway up` uploads the folder and builds it using the Dockerfile —
no GitHub repo needed.

## 4. Set the target URL and run time as variables
Either in the Railway dashboard (Project → your service → Variables),
or from the CLI before `railway up`:
```
railway variables --set "HOST_URL=https://devil-bhai-iyte.onrender.com"
railway variables --set "RUN_TIME=3h"
```

## 5. Don't expose a public domain
This is a background job, not a web server — skip "Generate Domain"
in the dashboard. It just runs, logs, and stops after 3 hours.

## 6. Watch it
```
railway logs
```
or the Deployments tab in the dashboard. Progress and the final
summary (requests/failures/avg/p95) print to the log; `errors.log`,
`report.html`, and `results_*.csv` are written inside the container.

## 7. Get the report files out afterward
The Dockerfile already dumps `errors.log` and the results CSV to the
container log when the run finishes, so `railway logs` (or the
dashboard) will show the error list and stats summary directly —
no need to pull files out of the container manually.

## Cost note
Railway bills for compute time on usage-based plans. A 3-hour run
with up to 20 concurrent bots is light CPU/RAM work, but check your
plan's included hours so this doesn't eat into them unexpectedly.
