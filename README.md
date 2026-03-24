<p align="center">
  <img alt="ReleaseBoard" src="docs/assets/logo-full.svg" width="420">
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"></a>
  <img src="https://img.shields.io/badge/tests-1141%20passed-22c55e?style=flat-square&logo=pytest&logoColor=white" alt="Tests: 1141 passed">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-6366f1?style=flat-square" alt="License: AGPL-3.0"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
</p>

<p align="center">
  <a href="https://buymeacoffee.com/polprog"><img src="https://img.shields.io/badge/Support%20this%20project-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Support this project"></a>
  <a href="https://github.com/sponsors/polprog-tech"><img src="https://img.shields.io/badge/GitHub%20Sponsors-ea4aaa?style=for-the-badge&logo=github-sponsors&logoColor=white" alt="GitHub Sponsors"></a>
</p>

<p align="center">
  <b>Know exactly where every repository stands before you ship.</b><br>
  <sub>Readiness scoring · Branch detection · Staleness alerts · Live dashboard · Static HTML export</sub>
</p>

<p align="center">
  <a href="#installation">Installation</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#screenshots">Screenshots</a> •
  <a href="#features">Features</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#dashboard">Dashboard</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#releasepilot-integration">ReleasePilot</a> •
  <a href="#troubleshooting">Troubleshooting</a> •
  <a href="#testing">Testing</a>
</p>

---

## What is ReleaseBoard?

ReleaseBoard is an internal release-readiness dashboard that analyzes your Git repositories against configurable release branch conventions. It works as both a **static HTML generator** and an **interactive web application** with live configuration editing, real-time analysis via Server-Sent Events, and a polished dashboard UI.

It answers critical questions before every release:

- Have all required release branches been created?
- Which repositories are missing the release branch?
- Which layers (UI / API / DB) are incomplete?
- Are any branches stale or incorrectly named?
- What is the overall release readiness score?

---

## Screenshots

### First-Run Setup Wizard

When no configuration file exists, ReleaseBoard guides you through creating your first release configuration with an intuitive setup wizard.

<p align="center">
  <img src="docs/assets/screenshots/first-run-setup.png" alt="ReleaseBoard — first-run setup wizard" width="800">
</p>

### Release Readiness Dashboard

Full interactive dashboard showing readiness scoring, layer breakdown, attention panel, metric cards, and release readiness summary — all in one view.

<p align="center">
  <img src="docs/assets/screenshots/dashboard-analysis.png" alt="ReleaseBoard — release readiness dashboard with analysis results" width="800">
</p>

### Prepare Config Wizard

Auto-discover repositories from GitHub or GitLab with the multi-step configuration wizard — define layers, scan organizations, review, and confirm.

<p align="center">
  <img src="docs/assets/screenshots/config-wizard.png" alt="ReleaseBoard — prepare config wizard with layer setup" width="800">
</p>

### Release Calendar (Dark Mode)

Plan and visualize your release schedule with the built-in calendar wizard — select months, configure display options, and export.

<p align="center">
  <img src="docs/assets/screenshots/release-calendar.png" alt="ReleaseBoard — release calendar wizard in dark mode" width="800">
</p>

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Web Server Mode](#web-server-mode)
- [API Endpoints](#api-endpoints)
- [Screenshots](#screenshots)
- [Features](#features)
- [Configuration](#configuration)
- [Usage](#usage)
- [Dashboard](#dashboard)
- [Architecture](#architecture)
- [ReleasePilot Integration](#releasepilot-integration)
- [OpsPortal Integration](#opsportal-integration)
- [GitLab CI/CD](#gitlab-cicd)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Author](#author)
- [License](#license)

## Installation

**Requirements:** Python 3.12+ and `git` CLI

```bash
# Clone the repository
git clone <your-repo-url> && cd ReleaseBoard

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate    # macOS / Linux
# .venv\Scripts\activate     # Windows

# Install (editable + dev deps)
pip install -e ".[dev]"

# Verify
releaseboard version
```

> **Corporate network (Zscaler / VPN)?** If `pip install` fails with SSL errors, see [Troubleshooting → SSL certificate errors](#ssl-certificate-errors-during-pip-install).

> **First run:** When no `releaseboard.json` exists, `releaseboard serve` opens a setup wizard to create your initial configuration. No manual config file creation is needed.

### Optional: ReleasePilot Integration

To enable the release-note wizard, install [ReleasePilot](https://github.com/polprog-tech/ReleasePilot):

```bash
pip install -e ".[releasepilot]"
```

ReleaseBoard works fully without ReleasePilot — the integration is optional. See [docs/releasepilot.md](docs/releasepilot.md) for details.

## Quick Start

### Standard Environment

```bash
# 1. Install (inside a virtual environment)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. (Optional) Set token for private repos
export GITLAB_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"   # GitLab
export GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"     # GitHub

# 3. Start the dashboard (setup wizard will guide you on first run)
releaseboard serve
# → Open http://127.0.0.1:8080

# Or create config manually from examples
cp examples/config.json releaseboard.json

# 4. Validate config
releaseboard validate --config releaseboard.json

# 5a. Generate static dashboard
releaseboard generate --config releaseboard.json
open output/dashboard.html

# 5b. Or start interactive dashboard
releaseboard serve --config releaseboard.json
```

### Corporate Network (Zscaler / VPN / Proxy)

If you're behind a corporate proxy that intercepts HTTPS (e.g. Zscaler, Netskope), you need to export the corporate CA bundle **before** installing dependencies. The examples below show GitLab, but the same applies to GitHub Enterprise or any private Git host.

<details>
<summary><b>macOS / Linux</b></summary>

```bash
# 1. Export corporate CA certificates (macOS)
security find-certificate -a -p \
  /Library/Keychains/System.keychain \
  /System/Library/Keychains/SystemRootCertificates.keychain \
  > ~/combined-ca-bundle.pem

# On Linux, the CA bundle is usually already available:
#   /etc/ssl/certs/ca-certificates.crt          (Debian/Ubuntu)
#   /etc/pki/tls/certs/ca-bundle.crt            (RHEL/Fedora)
# If your proxy adds its own CA, ask your IT department for the .pem file
# and append it: cat corporate-ca.pem >> ~/combined-ca-bundle.pem

# 2. Configure SSL trust (add to ~/.zshrc or ~/.bashrc to persist)
export SSL_CERT_FILE=~/combined-ca-bundle.pem
export REQUESTS_CA_BUNDLE=~/combined-ca-bundle.pem

# 3. (Optional) Also configure git to use the same CA bundle
git config --global http.sslCAInfo ~/combined-ca-bundle.pem

# 4. Install (inside a virtual environment)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 5. (Optional) Set token for private repos
export GITLAB_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"   # GitLab
export GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"     # GitHub Enterprise

# 6. Start the dashboard
releaseboard serve
# → Open http://127.0.0.1:8080
```
</details>

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
# 1. Export corporate CA certificate
# Ask your IT department for the corporate CA .pem file, or export it from
# certmgr.msc → Trusted Root Certification Authorities → Certificates
# Right-click → All Tasks → Export → Base-64 encoded X.509 (.CER)
# Save as: %USERPROFILE%\corporate-ca-bundle.pem

# 2. Configure SSL trust (add to your PowerShell profile to persist)
$env:SSL_CERT_FILE = "$env:USERPROFILE\corporate-ca-bundle.pem"
$env:REQUESTS_CA_BUNDLE = "$env:USERPROFILE\corporate-ca-bundle.pem"

# 3. (Optional) Also configure git to use the same CA bundle
git config --global http.sslCAInfo "$env:USERPROFILE\corporate-ca-bundle.pem"

# 4. Install (inside a virtual environment)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 5. (Optional) Set token for private repos
$env:GITLAB_TOKEN = "glpat-xxxxxxxxxxxxxxxxxxxx"   # GitLab
$env:GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxx"     # GitHub Enterprise

# 6. Start the dashboard
releaseboard serve
# → Open http://127.0.0.1:8080
```

> **Tip:** To make environment variables permanent on Windows, use `[System.Environment]::SetEnvironmentVariable("SSL_CERT_FILE", "$env:USERPROFILE\corporate-ca-bundle.pem", "User")` or set them via System Properties → Environment Variables.
</details>

## Web Server Mode

The web server is the **primary operating mode**. Start it with:

```bash
releaseboard serve                                    # defaults: 127.0.0.1:8080
releaseboard serve --port 9000 --host 0.0.0.0         # custom bind
releaseboard serve --config path/to/config.json        # custom config
releaseboard serve --verbose                           # debug logging
```

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--config` | `-c` | `releaseboard.json` | Path to configuration file |
| `--host` | `-h` | `127.0.0.1` | Bind address |
| `--port` | `-p` | `8080` | Bind port |
| `--verbose` | `-v` | — | Enable debug logging |

**First run:** When no configuration file exists, the server automatically presents a guided setup wizard.

For programmatic usage or ASGI deployment:

```python
from releaseboard.web.server import create_app

app = create_app(
    config_path="releaseboard.json",
    first_run=False,
    root_path="",
)
```

Use `root_path` when deploying behind a reverse proxy or embedding inside OpsPortal.

## API Endpoints

ReleaseBoard exposes 34 HTTP routes covering health monitoring, configuration management, analysis execution, export, and integrations.

**Health & Status**
- `GET /health/live` — liveness probe
- `GET /health/ready` — readiness probe
- `GET /api/status` — deep health status

**Configuration** — full CRUD with draft/save/reset workflow, JSON Schema validation, import/export, and branding overrides.

**Analysis** — trigger analysis runs, cancel in-progress analysis, stream real-time progress via SSE, and retrieve results.

**Export & Discovery** — static HTML export, repository discovery from root URLs.

**Integrations** — ReleasePilot capabilities, validation, and preparation endpoints; release calendar management.

See [docs/usage.md](docs/usage.md) for the full endpoint reference and examples.

## Features

### Release Intelligence

- **Readiness scoring** — overall and per-layer readiness percentage
- **Branch detection** — checks if expected release branches exist
- **Naming validation** — verifies branch names match configurable patterns
- **Staleness detection** — flags branches with no recent activity
- **Three-tier override** — branch patterns configurable globally, per-layer, and per-repository
- **Per-layer root URL** — `repository_root_url` per layer; precedence: repo URL → layer root → global root
- **GitHub API enrichment** — richer metadata (commit SHA, description, visibility, owner) via GitHub REST API
- **Smart provider routing** — auto-selects GitHub API or local git based on URL
- **URL → name derivation** — auto-derives repo display name from URL (strips `.git`, path, etc.)
- **Safe git access** — placeholder/example URLs are detected and skipped; no eager network calls on config edits
- **Error classification** — git errors classified into structured kinds (dns, auth, timeout, rate_limited, etc.) with concise user-facing messages; GitHub-specific HTTP errors mapped to precise kinds (404 → repo_not_found, 403 → rate_limited, 401 → auth_required)
- **Public GitHub repo support** — public repos work without `GITHUB_TOKEN` (with rate limits); HTTP errors no longer degrade into "Unknown error"
- **Git CLI fallback** — when the GitHub API is unavailable (rate-limited, network issues), falls back to `git ls-remote` for branch and default-branch detection; public repos remain analyzable without API access
- **Default-branch fallback** — when the release branch is missing but the repo is reachable, metadata is fetched from the default branch (last activity, visibility, default branch name)
- **Missing branch diagnostics** — reachable repos with missing release branches show "Missing Branch" status with actionable diagnostics (connectivity, default branch detected, expected pattern, analysis conclusion)
- **Provider metadata on missing branch** — repository metadata (default branch, visibility, description, web URL, owner) preserved even when the release branch doesn't exist
- **Deferred analysis model** — config validity, connectivity, and analysis are separate concerns; editing config never triggers network access

### Interactive Web Dashboard

- **Live analysis** — trigger analysis from the browser, see real-time progress via SSE
- **Stop/Cancel** — cancel a running analysis; UI immediately shows "Stopping…" state
- **Sticky toolbar** — primary action buttons (Analyze, Configuration, Export) remain visible while scrolling via sticky positioning
- **Config panel** — slide-out drawer with layer-grouped settings (repos grouped under layer headers)
- **Inline table actions** — Edit and Delete buttons directly in repo table rows
- **Delete confirmation** — modal with repo details before removing from config
- **Schema-driven validation** — live validation errors as you type
- **Draft/Save/Reset** — three-tier config state (persisted → active → draft)
- **Import/Export** — load and download JSON configs from the UI
- **Static export** — download a self-contained HTML snapshot
- **Validation scoping** — switching tabs clears stale validation messages; each tab triggers its own validation on entry
- **cfg-add-btn accent styling** — Add Layer/Add Repository buttons use orange accent (`#fb6400`) for better visibility

### Polished HTML Dashboard

- **Overview metrics** — total, ready, missing, stale, errors at a glance
- **Readiness ring** — visual readiness percentage with color coding
- **Charts** — status distribution doughnut and layer readiness bars via Chart.js
- **Layer sections** — grouped repository tables per layer with readiness bars
- **Drill-down modals** — click any repository for full detail
- **Filters & search** — filter by layer, status, naming validity, or search by name
- **Attention panel** — surfaces repositories needing immediate action
- **Summary report** — management-ready summary with suggested actions
- **Clickable brand/about** — click the app title to see author info in a polished modal
- **Drag-and-drop layout** — reorder dashboard sections via drag handles with drop placeholders
- **Visible drag handles** — drag handles show as distinct UI elements with grip icons, indented sections, and a layout-mode hint banner
- **Enhanced drop zones** — drop placeholders display "Drop here" text with accent-colored borders and glow
- **Layout templates** — 5 predefined templates (Default, Executive, Release Manager, Engineering, Compact) plus user-created templates
- **Immediate UI refresh** — add, edit, delete actions on repos instantly refresh the dashboard
- **Enterprise visual design** — technical color palette, reduced border-radius, refined status colors
- **Theme support** — light, dark, and system-auto with persistent preference
- **Print-friendly** — clean output when printed
- **Self-contained** — single HTML file with embedded CSS/JS

### Configuration

- **JSON config** — full JSON Schema validation
- **JSON editor validation** — JSON editor with real-time schema validation, parse error highlighting, and a field reference below the editor in a 2-column CSS layout
- **JSON autocomplete** — schema-aware autocomplete in the JSON editor suggests field names based on cursor context (root, release, layers, repositories, branding, settings, author, layout) with keyboard navigation (arrows, Tab, Enter, Escape)
- **Auto-name from URL** — new repos auto-derive display name from URL via real-time `oninput` derivation; manual edits are respected on subsequent URL changes
- **Branch pattern inheritance** — branch patterns show inheritance source (global / layer / repo override) with reset-to-inherited support; inherited values appear as placeholders when no explicit override is set
- **Effective value validation** — `validateDraft` checks auto-derived and inherited values (empty names, bare slugs without root URL)
- **Effective/Active tab** — third tab in the Configuration drawer showing resolved settings after inheritance and defaults, with source badges (global/layer/repo/derived/config/default) for full config transparency
- **Environment variable** — support for `${TOKEN}` placeholders
- **Configurable layers** — colors and display order
- **Configurable branding** — title, subtitle, company, accent color

## Configuration

Create a `releaseboard.json` file (see `examples/config.json` for a full example):

```json
{
  "release": {
    "name": "March 2025 Release",
    "target_month": 3,
    "target_year": 2025,
    "branch_pattern": "release/{MM}.{YYYY}"
  },
  "layers": [
    { "id": "ui",  "label": "Frontend", "color": "#3B82F6",
      "repository_root_url": "https://github.com/acme-frontend" },
    { "id": "api", "label": "Backend",  "color": "#10B981",
      "repository_root_url": "https://github.com/acme-backend",
      "branch_pattern": "release/{YYYY}.{MM}" }
  ],
  "repositories": [
    { "name": "web-app",  "url": "https://github.com/acme/web-app.git",  "layer": "ui" },
    { "name": "core-api", "url": "https://github.com/acme/core-api.git", "layer": "api" }
  ],
  "branding": {
    "title": "ReleaseBoard",
    "company": "Acme Inc.",
    "accent_color": "#4F46E5"
  },
  "settings": {
    "stale_threshold_days": 14,
    "theme": "system"
  },
  "author": {
    "name": "Jane Doe",
    "role": "Release Manager",
    "url": "https://github.com/janedoe",
    "tagline": "Keeping releases on track",
    "copyright": "© 2025 Acme Inc."
  }
}
```

### Branch Pattern Variables

| Variable | Description          | Example |
|----------|----------------------|---------|
| `{YYYY}` | 4-digit year        | `2025`  |
| `{YY}`   | 2-digit year        | `25`    |
| `{MM}`   | Zero-padded month   | `03`    |
| `{M}`    | Month without padding | `3`   |

### Three-Tier Override

Branch patterns resolve with this priority: **repository → layer → global**

```
Global:  release/{MM}.{YYYY}     → release/03.2025
API:     release/{YYYY}.{MM}     → release/2025.03   (layer override)
migrations: db-rel/{MM}.{YYYY}   → db-rel/03.2025    (repo override)
```

### Per-Layer Root URL

Each layer can define a `repository_root_url` so repositories don't need explicit URLs:

```
Precedence: repo explicit URL → layer root URL → global root URL
```

See [docs/configuration.md](docs/configuration.md) for the full reference.

## Usage

```bash
# Generate static dashboard (default: releaseboard.json → output/dashboard.html)
releaseboard generate

# Start interactive web dashboard
releaseboard serve

# Custom config and output
releaseboard generate --config my-config.json --output report.html

# Start on custom port
releaseboard serve --config my-config.json --port 9000

# Force dark theme
releaseboard generate --theme dark

# Verbose output
releaseboard generate --verbose

# Validate config only
releaseboard validate --config releaseboard.json
```

## Dashboard

The generated dashboard is a self-contained HTML file with:

| Section | Description |
|---------|-------------|
| **Readiness Ring** | Overall readiness percentage with color-coded ring |
| **Metric Cards** | Total, ready, missing, invalid, stale, error counts |
| **Charts** | Status distribution doughnut + layer readiness bar chart |
| **Filters** | Search, layer filter, status filter, naming filter |
| **Attention Panel** | Repos needing immediate action |
| **Layer Sections** | Per-layer tables with readiness bars |
| **Drill-Down** | Click any repo for full detail modal |
| **Summary Report** | Management-ready summary with suggested actions |

### Theme Support

The dashboard supports **light**, **dark**, and **system** (auto-detect) themes. The selected theme persists via localStorage.

## Architecture

ReleaseBoard uses a **dual-runtime architecture** — static HTML generator + interactive web app sharing the same core logic.

```
src/releaseboard/
├── cli/              # Typer CLI — generate, serve, validate, version
├── application/      # AnalysisService — shared pipeline for CLI and web
├── web/              # FastAPI server, app state, SSE events
├── config/           # JSON config loading + JSON Schema validation
├── domain/           # Core models + enums (zero dependencies)
├── analysis/         # Branch patterns, readiness, staleness, metrics
├── git/              # Abstract GitProvider + LocalGitProvider + GitHubProvider + SmartGitProvider
├── presentation/     # View models, theme, renderer, HTML template
└── shared/           # Logging, type aliases
```

**Key design principles:**

- **Clean layered architecture** — dependency direction: CLI/Web → Application → Domain
- **Zero-dependency domain** — domain layer has no external dependencies
- **Git provider abstraction** — git access behind a `GitProvider` protocol
- **Error classification** — git errors mapped to structured `GitErrorKind` enum with user-facing messages
- **JSON Schema validation** — config validated before processing
- **Deferred analysis** — config editing is decoupled from git network access
- **View model presentation** — presentation layer never touches domain directly
- **SSE progress** — live analysis progress (simpler than WebSocket for one-directional updates)
- **Three-tier config state** — persisted → active → draft
- **Layout system** — drag-and-drop sections with stable IDs and predefined templates

See [docs/architecture.md](docs/architecture.md) for the full architectural overview.

## ReleasePilot Integration

> **ReleaseBoard is designed to work together with [ReleasePilot](https://github.com/polprog-tech/ReleasePilot/blob/main/README.md)** — release note generation library. Together they form a complete release management toolkit.

When ReleasePilot is installed, ReleaseBoard provides a built-in wizard to generate structured, audience-targeted release notes directly from your Git history. The integration supports:

- **Multiple audiences** — technical, executive, customer-facing, changelog, and more
- **Multiple output formats** — Markdown, JSON, plain text, PDF, DOCX
- **Configurable scoping** — branch ranges, date filters, commit grouping
- **In-app preview and export** — generate, review, edit, and download without leaving the dashboard

The integration is optional — ReleaseBoard functions fully without ReleasePilot. Install it from [GitHub](https://github.com/polprog-tech/ReleasePilot) with `pip install -e ".[releasepilot]"` to enable the wizard.

See [`src/releaseboard/integrations/releasepilot/`](src/releaseboard/integrations/releasepilot/) for the adapter implementation and [docs/releasepilot.md](docs/releasepilot.md) for the full integration guide.

## GitLab CI/CD

ReleaseBoard can be used in GitLab CI/CD pipelines to generate release-readiness reports automatically on every push or scheduled run.

For a complete guide with pipeline examples, configuration, and artifact handling, see [docs/gitlab-cicd.md](docs/gitlab-cicd.md).

## OpsPortal Integration

ReleaseBoard integrates with [OpsPortal](../OpsPortal/) as a managed web service:

- **Auto-started** on port `8081` via `releaseboard serve --port 8081`
- **Health monitoring** — OpsPortal polls `GET /health/live`
- **Embedded** in the OpsPortal portal UI via iframe
- **Framing** enabled automatically via `RELEASEBOARD_ALLOW_FRAMING=true`

ReleaseBoard serves as the **reference architecture** for the OpsPortal platform. All four tools in the platform follow the same architectural model that ReleaseBoard established.

No additional configuration is needed — OpsPortal manages the lifecycle automatically.

## Troubleshooting

For detailed solutions to common issues, see [docs/troubleshooting.md](docs/troubleshooting.md).

| Problem | Quick Fix |
|---------|-----------|
| **SSL certificate errors** during `pip install` | Export corporate CA bundle: `export SSL_CERT_FILE=~/combined-ca-bundle.pem` — see [Corporate Network setup](#corporate-network-zscaler--vpn--proxy) |
| **`hatchling` not found** | `pip install --upgrade pip setuptools && pip install hatchling` |
| **GitLab repos return 404** | Set `GITLAB_TOKEN` via Setup Wizard or `export GITLAB_TOKEN="glpat-…"` |
| **Wrong GitLab hostname** | Check with `git remote -v` in a local clone and update `releaseboard.json` |
| **Analysis is slow** | Increase `max_concurrent` to `20` in settings, ensure token is set |
| **Port already in use** | `lsof -i :8080 -t \| xargs kill -9` or use `--port 9090` |

## Testing

ReleaseBoard has a comprehensive pytest suite covering the full stack.

```bash
# Run all tests
pytest

# With coverage
pytest --cov=releaseboard --cov-report=term-missing

# Run specific test file
pytest tests/test_readiness_analysis.py -v
```

**1141 tests** covering:

- Branch pattern resolution and validation
- Config loading, schema validation, and override logic
- Readiness analysis for all status paths
- Staleness detection edge cases
- Metrics aggregation
- HTML rendering and template output
- Theme handling
- Analysis service pipeline with progress callbacks
- Web state management (draft/save/reset/import/export)
- FastAPI endpoint behavior (config CRUD, analysis, export)
- Full end-to-end integration pipeline
- URL → name derivation edge cases
- GitHub URL parsing and provider selection
- Author config loading and schema validation
- Branch status fix (existing branch ≠ inactive)
- BranchInfo enriched metadata fields
- Error classification and GitErrorKind mapping (including RATE_LIMITED)
- GitHub HTTP error classification (404, 403, 401, network errors)
- Public GitHub repo handling without token
- Default-branch fallback when release branch is missing
- Provider metadata preservation on missing branch
- Diagnostics panel for reachable repos with missing branches
- Placeholder/example URL detection and skipping
- Deferred analysis model (config vs connectivity vs analysis validity)
- Drag-and-drop layout ordering and persistence
- Layout template selection and management
- Visible drag handles and enhanced drop zones
- JSON editor validation and field reference layout
- JSON autocomplete context-aware suggestions and keyboard navigation
- Auto-name from URL derivation (real-time oninput) and override tracking
- Branch pattern inheritance, reset-to-inherited, and inherited value placeholders
- Effective value validation (auto-derived and inherited values)
- Effective/Active tab resolved settings and source badges
- Validation scoping across tab switches
- Sticky toolbar positioning
- cfg-add-btn orange accent styling
- URL health-check endpoint

## Documentation

| Document | Description |
|----------|-------------|
| [docs/architecture.md](docs/architecture.md) | System architecture and design decisions |
| [docs/configuration.md](docs/configuration.md) | Full configuration reference |
| [docs/schema.md](docs/schema.md) | JSON Schema documentation |
| [docs/usage.md](docs/usage.md) | CLI usage guide |
| [docs/dashboard.md](docs/dashboard.md) | Dashboard features and sections |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common issues and solutions |
| [docs/gitlab-cicd.md](docs/gitlab-cicd.md) | GitLab CI/CD integration guide |
| [docs/security.md](docs/security.md) | Security considerations |
| [docs/testing.md](docs/testing.md) | Testing strategy and guidelines |

## Contributing

Contributions are welcome! Please read our [Contributing Guide](CONTRIBUTING.md) before submitting a pull request.

- [Contributing Guide](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)

## Author

Created and maintained by **POLPROG** ([@POLPROG](https://github.com/polprog-tech)).

## License

AGPL-3.0 — see [LICENSE](LICENSE)
