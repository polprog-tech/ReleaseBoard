# Troubleshooting

Common issues and their solutions when working with ReleaseBoard.

---

## SSL certificate errors during `pip install`

**Symptom:** `pip install -e ".[dev]"` fails with `SSL: CERTIFICATE_VERIFY_FAILED` or similar.

**Cause:** Corporate proxy (Zscaler, Netskope, etc.) intercepts HTTPS and uses its own CA certificate that Python doesn't trust.

**Fix — find and export your corporate CA bundle:**

```bash
# macOS — export system certificates (includes Zscaler CA)
security find-certificate -a -p \
  /Library/Keychains/System.keychain \
  /System/Library/Keychains/SystemRootCertificates.keychain \
  > ~/combined-ca-bundle.pem

# Tell pip (and Python) to use it
export SSL_CERT_FILE=~/combined-ca-bundle.pem
export REQUESTS_CA_BUNDLE=~/combined-ca-bundle.pem

# Now install works
pip install -e ".[dev]"
```

Add the `export` lines to your `~/.zshrc` (or `~/.bashrc`) to make it permanent.

**Quick check — is your venv working?**

```bash
cd ReleaseBoard
source .venv/bin/activate
pip install --dry-run requests 2>&1 | head -5
# Should show "Would install …" — not an SSL error
```

## `hatchling` not found during install

**Symptom:**
```
ERROR: Could not find a version that satisfies the requirement hatchling
```

**Cause:** `pip` can't download the build backend — usually an SSL/proxy issue (see above) or outdated pip.

**Fix:**
```bash
source .venv/bin/activate
pip install --upgrade pip setuptools
pip install hatchling
pip install -e ".[dev]"
```

## GitLab repositories return 404 (private repos)

**Symptom:** Analysis shows `Repository not found (HTTP 404)` for all repos, even though they exist.

**Cause:** GitLab returns 404 (not 401/403) for private repositories when the request is unauthenticated. This is by design — GitLab hides private repos from anonymous users.

**Fix — provide a GitLab Personal Access Token:**

Option A — **via the Setup Wizard** (recommended, persistent):

1. Open `http://127.0.0.1:8080`
2. Click ⚙️ → Setup Wizard
3. Select GitLab, paste your token (`glpat-…`) in the Token field
4. The token is saved in your browser's `localStorage` and automatically restored on every page load — even after server restarts

Option B — **via environment variable:**

```bash
export GITLAB_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"
releaseboard serve
```

> **How to create a token:** GitLab → Settings → Access Tokens → create with `read_api` scope (Reporter role or higher on the projects).

## Wrong GitLab hostname — all repos fail

**Symptom:** All repos return `HTTP 404` or `HTTP 0` (network error), but your token is correct.

**Cause:** The GitLab hostname in `releaseboard.json` doesn't match the actual server.

**Fix — verify the correct hostname from a local clone:**

```bash
cd your-project
git remote -v
# origin  https://gitlab.actual-host.com/group/project.git (fetch)
#                  ^^^^^^^^^^^^^^^^^^^^^^^^
#                  This is the correct hostname
```

Then update all URLs in `releaseboard.json`:

```bash
cd ReleaseBoard
sed -i '' 's|gitlab.wrong-host.com|gitlab.actual-host.com|g' releaseboard.json
```

## Analysis is slow (>30 seconds for ~30 repos)

**Expected performance:** ~15–25s for 33 repos over a corporate VPN (each API call takes ~1–1.5s due to proxy latency).

**If it's significantly slower, check:**

1. **`max_concurrent` too low** — default is `10`, increase to `20` in Settings:
   ```json
   "settings": {
     "max_concurrent": 20,
     "timeout_seconds": 10
   }
   ```

2. **Token not set** — without a token, GitLab returns 404 for every repo (15s timeout each). See [GitLab repositories return 404](#gitlab-repositories-return-404-private-repos).

3. **Network latency** — ReleaseBoard makes 1–2 API calls per repo (branch check + tag enrichment). Over a high-latency VPN, each call may take 1–2s. This is the main bottleneck — the analysis itself is concurrent.

## Server won't start — `address already in use`

```bash
# Find and kill whatever is using port 8080
lsof -i :8080 -t | xargs kill -9

# Or use a different port
releaseboard serve --port 9090
```
