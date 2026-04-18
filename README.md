# Voice Photo Search

A bridge service between Home Assistant voice commands (via Wyoming satellites + Azure OpenAI) and Immich photo display on ImmichFrame picture panels.

## Architecture

```
Wyoming Satellite → Azure STT → Azure OpenAI (function calling)
                                      ↓
                          voice-photo-search API (Flask)
                                      ↓
                              NLP Query Parser
                    (extracts person names + dates from
                     natural language, strips filler words)
                                      ↓
                    ┌─────────────────┼─────────────────┐
                    ↓                 ↓                 ↓
              CLIP Search      Person Search      Date Search
            /api/search/smart  /api/search/metadata (combinable)
                    └─────────────────┼─────────────────┘
                                      ↓
                          Voice Search Album (populated)
                                      ↓
                          ImmichFrame (displays album on tablets)
```

## Components

| Component | Host | Port | Purpose |
|-----------|------|------|---------|
| voice-photo-search | `<SEARCH_HOST>` | 8008 | API proxy between HA and Immich |
| Immich | `<IMMICH_HOST>` | 2283 | Photo library + CLIP smart search |
| ImmichFrame | `<FRAME_HOST>` | 8080 | Digital photo frame server |
| Home Assistant | `<HA_HOST>` | 8123 | Voice pipeline + Extended OpenAI Conversation |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/search` | POST | Multi-mode search → populate album (see below) |
| `/api/restore` | POST | Restore album to random photo rotation |
| `/health` | GET | Health check |

### Search Modes

The `/api/search` endpoint accepts a JSON body. The primary mode is **natural language via `query`** — the server parses person names and dates automatically:

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string | Natural language query — NLP extracts person names and dates automatically. Remainder goes to CLIP. |
| `person` | string | (Direct mode) Person name, comma-separated for multiple (e.g. "Alice", "Alice, Bob") |
| `date_from` | string | (Direct mode) Start date in YYYY-MM-DD format |
| `date_to` | string | (Direct mode) End date in YYYY-MM-DD format |

**NLP routing (when only `query` is provided):**
- `"photos of Alice"` → detects person "Alice" → metadata search by person
- `"Alice from April 16"` → detects person + date → metadata search with both filters
- `"photos from yesterday"` → detects date only → metadata search by date
- `"sunset at the beach"` → no person/date detected → CLIP visual search
- `"Alice at the park"` → detects person "Alice", remainder "park" → metadata search (CLIP portion discarded when person/date found)

**Direct routing (when `person`/`date_from`/`date_to` provided explicitly):**
- Structured params bypass NLP — useful for curl/API testing
- If `person` and/or `date_from`/`date_to` are provided → metadata search
- If only `query` with no structured params → NLP parsing → routed as above

Known people (matched case-insensitively, longest-first): auto-detected from your Immich library's named people.

## Setup

### 1. Create Voice Search Album in Immich

```bash
curl -X POST http://<IMMICH_HOST>:2283/api/albums \
  -H "x-api-key: YOUR_IMMICH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"albumName":"Voice Search","description":"Dynamic album populated by voice commands via Home Assistant"}'
```

Save the returned `id` — you'll need it for `VOICE_SEARCH_ALBUM_ID`.

### 2. Deploy voice-photo-search

Copy `app.py`, `Dockerfile`, and `docker-compose.yml` to the target host:

```bash
mkdir -p ~/docker/voice-photo-search
# copy files into ~/docker/voice-photo-search/
cd ~/docker/voice-photo-search
```

Edit `docker-compose.yml` environment variables:

| Variable | Description |
|----------|-------------|
| `IMMICH_URL` | Immich server URL (e.g. `http://immich-host:2283`) |
| `IMMICH_API_KEY` | Immich API key with album + asset permissions |
| `VOICE_SEARCH_ALBUM_ID` | UUID of the Voice Search album created in step 1 |
| `MAX_RESULTS` | Max photos returned per search (default: 20) |
| `DEFAULT_ALBUM_SIZE` | Number of random photos for normal rotation (default: 250) |

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
# edit .env with your IMMICH_URL, IMMICH_API_KEY, VOICE_SEARCH_ALBUM_ID
```

Build and start:

```bash
docker compose up -d --build
```

Verify:

```bash
curl http://localhost:8008/health
# {"status":"ok"}
```

### 3. Seed Album with Random Photos

Before switching ImmichFrame to album mode, populate the album so frames aren't empty:

```bash
curl -X POST http://localhost:8008/api/restore
```

### 4. Configure ImmichFrame

Edit `~/docker/immichframe/config/Settings.yml`:

```yaml
Albums:
  - <YOUR_VOICE_SEARCH_ALBUM_ID>
RefreshAlbumPeopleInterval: 0
```

Mount the config in `docker-compose.yml`:

```yaml
volumes:
  - ./config/Settings.yml:/app/Config/Settings.yml:ro
```

Do NOT use `env_file` — environment variables override Settings.yml.

Restart ImmichFrame:

```bash
cd ~/docker/immichframe
docker compose up -d
```

**Note:** `RefreshAlbumPeopleInterval: 0` means ImmichFrame re-reads the album on every request (supported since PR #441). Use `docker compose up -d` (not `restart`) after compose file changes.

### 5. Configure Home Assistant

In HA: Settings → Integrations → Extended OpenAI Conversation → Configure → Functions

See `ha-tool-config.yaml` for the full reference. Key points:

```yaml
- spec:
    name: search_photos_for_frames
    description: >
      Search for photos using natural language and display them on the picture
      frames around the house.
    parameters:
      type: object
      properties:
        query:
          type: string
          description: >-
            Natural language description of the photos to find.
      required:
        - query
  function:
    type: rest
    resource: "http://<SEARCH_HOST>:8008/api/search"
    method: POST
    headers:
      Content-Type: "application/json"
    payload: >-
      {"query":"{{query}}"}
```

**Important — `payload` must be a string, not a YAML mapping:**

```yaml
# ✅ CORRECT — string payload (use >- block scalar or quoted string)
payload: >-
  {"query":"{{query}}"}

# ❌ WRONG — YAML mapping (was supported in older HA versions, now fails)
payload:
  query: "{{ query }}"
# Error: "value should be a string for dictionary value @ data['payload']"
```

**Notes:**
- Do NOT include `value_template` — it triggers a false warning in the HA UI.
- After updating tool definitions, **reload the integration** (three-dot menu → Reload).
- The raw JSON response is passed to Azure OpenAI which summarizes it naturally.

## Testing

```bash
# CLIP visual search
curl -s -X POST http://localhost:8008/api/search \
  -H "Content-Type: application/json" \
  -d '{"query":"parks"}'
# {"count":20,"message":"Loaded 20 photos matching 'parks' to your picture frames..."}

# Person search
curl -s -X POST http://localhost:8008/api/search \
  -H "Content-Type: application/json" \
  -d '{"person":"Alice"}'
# {"count":20,"message":"Loaded 20 photos of Alice to your picture frames..."}

# Date range search
curl -s -X POST http://localhost:8008/api/search \
  -H "Content-Type: application/json" \
  -d '{"date_from":"2025-12-25","date_to":"2025-12-25"}'
# {"count":20,"message":"Loaded 20 photos from 2025-12-25 to your picture frames..."}

# Combined: person + date
curl -s -X POST http://localhost:8008/api/search \
  -H "Content-Type: application/json" \
  -d '{"person":"Alice","date_from":"2025-12-25","date_to":"2025-12-25"}'
# {"count":5,"message":"Loaded 5 photos of Alice from 2025-12-25..."}

# Restore
curl -s -X POST http://localhost:8008/api/restore
# {"added":250,"message":"Picture frames restored to normal rotation with 250 random photos.","removed":5}
```

### Testing via HA Actions

In Developer Tools → Actions:

```yaml
action: conversation.process
data:
  agent_id: conversation.extended_openai_conversation
  text: "Show me sunset photos on the picture frames"
```

### Voice

- Say: "Show me pictures from the aquarium"
- Say: "Go back to normal photos" to restore

## How It Works

1. **Search flow:** Voice command → Azure OpenAI calls `search_photos_for_frames` with a single `query` string → API runs NLP parsing:
   - `parse_natural_query()` extracts person names (matched against Immich's `/api/people`, case-insensitive, longest-first to handle compound names like "Grand-Dad" before "Dad") and dates (via `dateparser` for natural language like "yesterday", "last week", "April 16")
   - Filler words stripped: photos, pictures, images, show, me, find, display, of, with, and, the, on, from, since, in, during, taken, frames, picture
   - If person and/or date detected → Immich `/api/search/metadata`
   - If no person/date detected → remainder goes to Immich `/api/search/smart` (CLIP visual similarity)
   → Clears Voice Search album → populates with matching asset IDs → ImmichFrame picks up new album contents on next request
2. **Restore flow:** Voice command → Azure OpenAI calls `restore_picture_frames` → proxy clears album → fetches random assets from Immich → populates album → frames return to normal rotation

## Files

| File | Description |
|------|-------------|
| `app.py` | Flask API service with NLP query parsing (search, restore, health endpoints) |
| `Dockerfile` | Python 3.12 slim + Flask + gunicorn + dateparser |
| `docker-compose.yml` | Docker Compose config with environment variables (incl. TZ for dateparser) |
| `ha-tool-config.yaml` | Reference for HA Extended OpenAI Conversation tool definitions |

## Gotchas

- **`docker compose restart` vs `up -d`:** `restart` doesn't re-read compose file changes (volumes, env). Always use `docker compose up -d` after compose edits.
- **`docker compose up -d --build`:** Needed after changing `app.py` since the Dockerfile `COPY`s it into the image.
- **ImmichFrame env_file vs Settings.yml:** If both are present, env vars override Settings.yml. Don't use `env_file`.
- **HA payload format:** Must be a string (not YAML mapping) in current Extended OpenAI Conversation versions.
- **CLIP has no relevance cutoff:** Immich returns top-N results ranked by similarity but with no score. Keep `MAX_RESULTS` low (20) to avoid irrelevant results.

## Future Enhancements

- [ ] Android TV integration (show photos on TV via Immich app deep link)
- [x] Date-filtered search ("show me photos from last Christmas")
- [x] Person search ("show me photos of [name]")
- [x] Server-side NLP parsing — single `query` param handles person names + dates automatically
- ~~HA tool definition for multi-param search~~ — not needed; NLP server-side parsing is simpler and more reliable
- [ ] Reduce ImmichFrame refresh delay (trigger browser reload via HA)
