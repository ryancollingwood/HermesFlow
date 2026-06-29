# Upstream Data-Source Blocking Patterns

Pipelines that fetch from web APIs (RSS, REST, scraping) may hit blocking from the upstream provider. Common scenarios and approaches:

## Reddit

Reddit aggressively blocks cloud-hosted IP ranges (AWS, GCP, DigitalOcean, Hetzner, etc.) from accessing both RSS feeds and the JSON API. Patterns:

| Attempt | Result |
|---------|--------|
| `curl` to `reddit.com/r/X/top.rss?t=today` | 403 or 429 (Cloudflare block) |
| `curl` with real User-Agent | Same block |
| `curl` to `old.reddit.com/r/X/top.json` | Same block |
| Browser via Browserbase | Blocked (no residential proxy) |
| `feedparser` or `requests` library | Same block from same IP |

**Why the pipeline still works:** the Windmill worker likely runs on a different host/IP that isn't blocked. The pipeline code importing `feedparser` or calling the API from the worker should succeed even though the Hermes container fails.

**If the worker is also blocked:**
1. **Reddit API (praw)** — register a Reddit script app, use `praw.Reddit(client_id=..., client_secret=..., user_agent=...)`. Add the `praw` dep to the Windmill lock file.
2. **Residential proxy** — route Windmill worker requests through a proxy server on a residential IP.
3. **Old.reddit.com HTML scraping** — less aggressive blocking, but fragile and not recommended.

## Rate-Limiting (General)

Many APIs use per-IP rate limits. The `dlt` framework handles retries internally via `tenacity`, but for aggressive sources you may need:

```python
import time
from dlt.sources.helpers import requests

@dlt.resource(name="data", write_disposition="append")
def fetch_with_retry():
    for page in range(1, 10):
        resp = requests.get(f"https://api.example.com/items?page={page}")
        resp.raise_for_status()
        yield resp.json()
        time.sleep(1)  # polite delay
```

## RSS Feeds

Most RSS feeds (WordPress, Substack, Medium, etc.) are not blocked. Feedparser handles encoding, CDATA, and namespace quirks automatically. For authenticated feeds, pass the URL with credentials inline or set headers via `feedparser.parse(url, agent=user_agent)`.
