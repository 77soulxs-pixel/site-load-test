"""
3-hour website soak test: 10-20 bots that crawl, browse, submit forms,
probe errors, and hit APIs. Only run against a site you own / have
permission to test.

NOTE ON RENDER FREE TIER: if your site runs on Render's free plan, it
spins down when idle (cold start can take 30-60s) and has limited
CPU/RAM. Sustained 20-bot load can be heavier than the plan is built
for and may just produce timeouts rather than useful results, or eat
your instance hours. Consider starting with fewer users (see
--users below) and watching the first 10-15 minutes before leaving it
to run unattended.

Setup:
    pip install locust beautifulsoup4

Run (3 hours, headless, with reports):
    locust -f locustfile.py --headless \
        --host https://devil-bhai-iyte.onrender.com \
        --run-time 3h --html report.html --csv results --logfile locust.log

Keep it alive after you disconnect (Linux/Mac):
    nohup locust -f locustfile.py --headless \
        --host https://devil-bhai-iyte.onrender.com \
        --run-time 3h --html report.html --csv results > run.out 2>&1 &

Start cautious (fixed 5 users, ignore the Shape class) to sanity-check
the site handles the bots before scaling up:
    locust -f locustfile.py --headless \
        --host https://devil-bhai-iyte.onrender.com \
        --users 5 --spawn-rate 1 --run-time 15m --html report.html

Optional env vars:
    SLOW_SECONDS=3        flag any response slower than this
    SUBMIT_FORMS=1        set 0 to skip form submissions
    MAX_PAGES=300         cap on pages the crawler tracks
    API_PATHS=/api/health,/api/items   comma-separated JSON endpoints to test

LOGIN (to let bots test pages behind your login):
    Create accounts.txt next to this file, one test account per line:
        user1@example.com:Password123
        user2@example.com:Password123
        user3@example.com:Password123
    Use REAL test accounts you created yourself, not guessed credentials.
    Each bot picks one account (round-robin) and logs in once at start.

    You don't need to know the login URL or field names - each bot will:
      1. Look for a login link on the homepage (text/href containing
         "login", "signin", "sign-in", "log-in"); falls back to trying
         /login, /signin, /auth/login, /account/login, /users/sign_in.
      2. Parse whatever form on that page has a password field, read
         its field names and any hidden/CSRF token automatically, and
         submit it with the account's credentials.
      3. Check whether login looks like it worked (the password field
         is gone from the next page, or a Set-Cookie came back) and
         log a warning to errors.log if not - it still carries on as
         a logged-out bot rather than stopping the whole run.

    Override the guess if needed:
        LOGIN_PATH=/my/login/path
        LOGIN_USER_FIELD=email   LOGIN_PASS_FIELD=password
"""
import logging
import os
import random
import string
import threading
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup
from locust import HttpUser, LoadTestShape, between, events, task

SLOW = float(os.getenv("SLOW_SECONDS", "3"))
SUBMIT_FORMS = os.getenv("SUBMIT_FORMS", "1") == "1"
MAX_PAGES = int(os.getenv("MAX_PAGES", "300"))
API_PATHS = [p.strip() for p in os.getenv("API_PATHS", "").split(",") if p.strip()]
TOTAL_SECONDS = 3 * 3600
LOGIN_PATH = os.getenv("LOGIN_PATH", "")
LOGIN_USER_FIELD = os.getenv("LOGIN_USER_FIELD", "")
LOGIN_PASS_FIELD = os.getenv("LOGIN_PASS_FIELD", "")
LOGIN_PATH_GUESSES = ["/login", "/signin", "/sign-in", "/auth/login",
                      "/account/login", "/users/sign_in", "/user/login"]


def load_accounts():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.txt")
    accounts = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                user, _, pw = line.partition(":")
                accounts.append((user.strip(), pw.strip()))
    return accounts


ACCOUNTS = load_accounts()
ERROR_MARKERS = ("traceback (most recent call", "internal server error",
                 "unhandled exception", "stack trace", "fatal error")

err_log = logging.getLogger("errors")
err_log.setLevel(logging.INFO)
_fh = logging.FileHandler("errors.log")
_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
err_log.addHandler(_fh)


class Site:
    """State shared by all bots."""
    lock = threading.Lock()
    pages = {"/"}
    queue = ["/"]
    forms = []          # (page_path, form_index)
    form_keys = set()
    assets = set()


def rand_text(n=8):
    return "".join(random.choices(string.ascii_lowercase, k=n))


NASTY = [
    "", " ", "A" * 5000, "' OR '1'='1", "<script>alert(1)</script>",
    "../../etc/passwd", "%00", "\u202e\u0000", "😀" * 50, "-1", "99999999999999999999",
    "{{7*7}}", "${7*7}", "null", "undefined", "\n\r\n",
]


class Bot(HttpUser):
    wait_time = between(1, 5)

    # ---------- helpers ----------
    def _path(self, url):
        p = urlparse(url)
        return p.path + (f"?{p.query}" if p.query else "") or "/"

    def req(self, method, path, name=None, ok_below=400, check_body=True, **kw):
        """Request with validation. Fails on bad status, slowness, or error text."""
        with self.client.request(method, path, name=name or path,
                                 catch_response=True, **kw) as r:
            problem = None
            if r.status_code == 0:
                problem = "no response / connection error"
            elif r.status_code >= ok_below:
                problem = f"status {r.status_code}"
            elif r.elapsed.total_seconds() > SLOW:
                problem = f"slow {r.elapsed.total_seconds():.1f}s"
            elif check_body and "text" in r.headers.get("Content-Type", ""):
                low = r.text[:20000].lower()
                for m in ERROR_MARKERS:
                    if m in low:
                        problem = f"error text on page: '{m}'"
                        break
            if problem:
                r.failure(problem)
                err_log.info("%s %s -> %s", method, path, problem)
            else:
                r.success()
            return r

    def learn(self, r, page_path):
        """Extract links, assets, forms from an HTML response."""
        if r is None or r.status_code >= 400 or "html" not in r.headers.get("Content-Type", ""):
            return
        soup = BeautifulSoup(r.text, "html.parser")
        host = urlparse(self.host).netloc
        base = self.host.rstrip("/") + page_path
        with Site.lock:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                u = urldefrag(urljoin(base, href))[0]
                if urlparse(u).netloc == host and len(Site.pages) < MAX_PAGES:
                    p = self._path(u)
                    if p not in Site.pages:
                        Site.pages.add(p)
                        Site.queue.append(p)
            for tag, attr in (("img", "src"), ("script", "src"), ("link", "href")):
                for t in soup.find_all(tag, **{attr: True}):
                    u = urljoin(base, t[attr])
                    if urlparse(u).netloc == host:
                        Site.assets.add(self._path(u))
            for i, _ in enumerate(soup.find_all("form")):
                key = (page_path, i)
                if key not in Site.form_keys:
                    Site.form_keys.add(key)
                    Site.forms.append(key)

    # ---------- login ----------
    _account_counter = {"i": 0}
    _account_lock = threading.Lock()

    def _next_account(self):
        if not ACCOUNTS:
            return None
        with Bot._account_lock:
            i = Bot._account_counter["i"]
            Bot._account_counter["i"] += 1
        return ACCOUNTS[i % len(ACCOUNTS)]

    def _find_login_path(self, home_soup):
        if LOGIN_PATH:
            return LOGIN_PATH
        for a in home_soup.find_all("a", href=True):
            blob = (a["href"] + " " + a.get_text()).lower()
            if any(k in blob for k in ("login", "sign in", "signin", "log in", "log-in")):
                return self._path(urljoin(self.host.rstrip("/") + "/", a["href"]))
        for guess in LOGIN_PATH_GUESSES:
            with self.client.get(guess, name="[find login]", catch_response=True) as r:
                r.success()  # guesses never count as failures either way
                if r.status_code < 400:
                    return guess
        return None

    def _login(self):
        account = self._next_account()
        if not account:
            return
        username, password = account

        home = self.req("GET", "/", name="/ (start)")
        self.learn(home, "/")
        soup = BeautifulSoup(home.text, "html.parser")
        login_path = self._find_login_path(soup)
        if not login_path:
            err_log.info("Could not locate a login page (set LOGIN_PATH to override)")
            return

        r = self.req("GET", login_path, name="[login page]")
        if r.status_code >= 400:
            err_log.info("Login page %s returned %s", login_path, r.status_code)
            return
        lsoup = BeautifulSoup(r.text, "html.parser")
        form = None
        for f in lsoup.find_all("form"):
            if f.find("input", {"type": "password"}):
                form = f
                break
        if form is None:
            err_log.info("No form with a password field found on %s", login_path)
            return

        data = {}
        user_field = pass_field = None
        for el in form.find_all("input"):
            name = el.get("name")
            if not name:
                continue
            t = (el.get("type") or "text").lower()
            if t == "password":
                pass_field = name
            elif t == "hidden":
                data[name] = el.get("value", "")
            elif t in ("text", "email") and user_field is None:
                user_field = name
        user_field = LOGIN_USER_FIELD or user_field
        pass_field = LOGIN_PASS_FIELD or pass_field
        if not user_field or not pass_field:
            err_log.info("Could not determine username/password field names on %s "
                        "(set LOGIN_USER_FIELD / LOGIN_PASS_FIELD)", login_path)
            return
        data[user_field] = username
        data[pass_field] = password

        action = urljoin(self.host.rstrip("/") + login_path, form.get("action") or login_path)
        method = (form.get("method") or "post").upper()
        path = self._path(action)
        kw = {"params": data} if method == "GET" else {"data": data}
        resp = self.req(method, path, name="[login submit]", ok_below=500,
                        check_body=False, **kw)

        still_has_password_field = "type=\"password\"" in resp.text.lower() if resp.text else False
        if resp.status_code >= 400 or still_has_password_field:
            err_log.info("Login looked unsuccessful for %s on %s (status %s)",
                        username, login_path, resp.status_code)
        else:
            self.learn(resp, self._path(resp.url) if resp.url else login_path)

    # ---------- lifecycle ----------
    def on_start(self):
        if ACCOUNTS:
            self._login()
        else:
            r = self.req("GET", "/", name="/ (start)")
            self.learn(r, "/")
        home = self.client.get("/", name="/ (header check)", catch_response=True)
        with home:
            for h in ("Strict-Transport-Security", "X-Content-Type-Options"):
                if h not in home.headers:
                    err_log.info("Missing security header: %s", h)
            home.success()

    # ---------- tasks ----------
    @task(5)
    def crawl(self):
        with Site.lock:
            path = Site.queue.pop(0) if Site.queue else None
        if path:
            r = self.req("GET", path, name="[crawl]")
            self.learn(r, path)

    @task(10)
    def browse(self):
        with Site.lock:
            path = random.choice(list(Site.pages))
        r = self.req("GET", path, name="[browse]")
        self.learn(r, path)

    @task(3)
    def assets(self):
        with Site.lock:
            a = random.choice(list(Site.assets)) if Site.assets else None
        if a:
            self.req("GET", a, name="[asset]", check_body=False)

    @task(2)
    def not_found(self):
        self.req("GET", f"/{rand_text(12)}/{rand_text(6)}", name="[expect 404]",
                 ok_below=500)

    @task(2)
    def malformed_urls(self):
        bad = random.choice([
            "/%00", "/%zz", "/../../etc/passwd", "/." * 50,
            "/?q=" + "A" * 5000, "/?a[]=1&a[b]=2", "/??&&==",
            "/" + "a" * 3000, "/%E0%A4%A", "/?q=<script>alert(1)</script>",
        ])
        self.req("GET", bad, name="[malformed url]", ok_below=500)

    @task(1)
    def odd_methods(self):
        with Site.lock:
            path = random.choice(list(Site.pages))
        m = random.choice(["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
        self.req(m, path, name=f"[{m} on page]", ok_below=500, check_body=False)

    def _submit(self, valid):
        if not SUBMIT_FORMS:
            return
        with Site.lock:
            if not Site.forms:
                return
            page, idx = random.choice(Site.forms)
        r = self.req("GET", page, name="[form page]")
        if r.status_code >= 400:
            return
        forms = BeautifulSoup(r.text, "html.parser").find_all("form")
        if idx >= len(forms):
            return
        form = forms[idx]
        data = {}
        for el in form.find_all(["input", "textarea", "select"]):
            name = el.get("name")
            if not name:
                continue
            t = (el.get("type") or el.name).lower()
            if t in ("submit", "button", "image", "reset", "file"):
                continue
            if t == "hidden":
                data[name] = el.get("value", "")
            elif t == "checkbox":
                data[name] = el.get("value", "on")
            elif t == "radio":
                data.setdefault(name, el.get("value", "1"))
            elif el.name == "select":
                opt = el.find("option")
                data[name] = opt.get("value", "") if opt else ""
            elif not valid:
                data[name] = random.choice(NASTY)
            elif t == "email":
                data[name] = f"bot_{rand_text(6)}@example.com"
            elif t == "number":
                data[name] = str(random.randint(1, 10))
            elif t == "tel":
                data[name] = "5551234567"
            elif t == "url":
                data[name] = "https://example.com"
            elif t == "password":
                data[name] = "Test1234!x"
            else:
                data[name] = f"bot {rand_text(8)}"
        action = urljoin(self.host.rstrip("/") + page, form.get("action") or page)
        method = (form.get("method") or "get").upper()
        path = self._path(action)
        label = "valid" if valid else "invalid"
        kw = {"params": data} if method == "GET" else {"data": data}
        # invalid input may legitimately return 4xx; only 5xx is a failure
        self.req(method, path, name=f"[form {label}]",
                 ok_below=400 if valid else 500, **kw)

    @task(3)
    def form_valid(self):
        self._submit(True)

    @task(3)
    def form_invalid(self):
        self._submit(False)

    @task(2)
    def api(self):
        if not API_PATHS:
            return
        p = random.choice(API_PATHS)
        r = self.req("GET", p, name=f"[api] {p}", check_body=False)
        if r.status_code < 400 and "json" in r.headers.get("Content-Type", ""):
            try:
                r.json()
            except Exception:
                err_log.info("Invalid JSON from %s", p)


class Shape(LoadTestShape):
    """Alternates 10 and 20 bots every 30 minutes for 3 hours."""

    def tick(self):
        t = self.get_run_time()
        if t > TOTAL_SECONDS:
            return None
        users = 10 if int(t // 1800) % 2 == 0 else 20
        return users, 2


@events.test_stop.add_listener
def summary(environment, **kw):
    s = environment.stats.total
    print(f"\nRequests: {s.num_requests}  Failures: {s.num_failures}  "
          f"Avg: {s.avg_response_time:.0f}ms  p95: {s.get_response_time_percentile(0.95):.0f}ms")
    print("Details in errors.log, report.html, results_*.csv")
