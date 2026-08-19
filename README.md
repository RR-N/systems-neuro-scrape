# systems-neuro-scrape

Scrapes [systems-neuroscience Google Group](https://groups.google.com/g/systems-neuroscience)
(neuroscience job postings and announcements) messages and packages them into a public JSON file, updated daily by GitHub Actions. Intended for use by developer platforms with strict domain approval and enforced adherence to public API endpoints (specifically, /r/neuroscience's Carberry-3000 bot).

- **Output:** [`data/messages.json`](data/messages.json)
- **Schedule:** daily at 05:23 UTC via [`.github/workflows/scrape.yml`](.github/workflows/scrape.yml)

## How it works

The modern Google Groups UI renders its data as JSON inside `AF_initDataCallback(...)` script blocks, and paginates the topic list through a `batchexecute` RPC:

1. `GET /g/<group>` — first 30 topics (newest activity first), total topic count, + continuation token.
2. `POST /_/GroupsFrontendUi/data/batchexecute` (RPC id `Dq0xse`) — follows token chain through the full topic list.
3. `GET /g/<group>/c/<topic-id>` — each topic page features every full HTML bodies, authors, & timestamps.

## Output format

```jsonc
{
  "meta": {
    "group": "systems-neuroscience",
    "group_email": "systems-neuroscience@googlegroups.com",
    "group_url": "https://groups.google.com/g/systems-neuroscience",
    "scraped_at": "2026-08-19T02:50:04Z",
    "topics_reported_by_google": 2763,
    "topics_in_file": 2763,
    "topics_pending_bodies": 0,
    "complete": true
  },
  "topics": [                       
    {
      "id": "4NXhLb9CHaE",
      "url": "https://groups.google.com/g/systems-neuroscience/c/9NXhLb5CHuE",
      "title": "Psychoceramic Postdoctoral Fellowships ...",
      "snippet": "Psychoceramics Postdoctoral Fellowships are open ...",
      "created": "2026-08-04T12:43:37Z",
      "created_ts": 1785847417,
      "last_activity": "2026-08-04T22:14:41Z",
      "last_activity_ts": 1785881681,
      "num_messages": 1,
      "authors": ["Josiah Carberry"],
      "messages": [                 
        {
          "id": "jISyG3VpBAAJ",
          "author": "Josiah Carberry",
          "author_id": "108294806036313288641",
          "created": "2026-08-04T12:43:37Z",
          "created_ts": 1785847417,
          "updated": "2026-08-04T22:14:41Z",
          "title": "Psychoceramics Postdoctoral Fellowships ...",
          "body_html": "<p ...>full message HTML</p>",
          "body_text": "plain-text rendering of the body"
        }
      ]
    }
  ]
}
```