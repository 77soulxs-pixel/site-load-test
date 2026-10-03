FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir locust beautifulsoup4

COPY locustfile.py accounts.txt ./

# HOST_URL and RUN_TIME are set as Railway variables (see README).
# After the run, dump errors.log and the results CSV into the
# container log so `railway logs` shows the findings without needing
# to pull files out of the container.
CMD locust -f locustfile.py --headless --host "$HOST_URL" \
    --run-time "${RUN_TIME:-3h}" \
    --html report.html --csv results --logfile locust.log; \
    echo "=== ERRORS LOG ==="; cat errors.log 2>/dev/null || echo "(none)"; \
    echo "=== RESULTS SUMMARY ==="; cat results_stats.csv 2>/dev/null || echo "(none)"
