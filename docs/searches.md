# Search configuration reference

Searches are defined in `searches.yaml` as a YAML list. Each entry is one
independent alert that polls eBay on its own interval, optionally analyses new
listings with a local Ollama LLM, and sends a Pushover notification.

Searches can be created and edited either through the **web UI** (default
`http://localhost:8787`) or by editing this file directly. UI changes take
effect immediately and are written back to the file; manual file edits are
picked up on the next restart. Note the UI rewrites the file on every change,
so hand-written comments in it will be lost.

```yaml
- id: my-search
  name: "Human label"
  query: "search keywords"
  # ... all other fields are optional
```

---

## Top-level fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | **required** | Stable slug used as the dedupe namespace in SQLite. Use lowercase letters, numbers, and hyphens. **Changing this value resets seen-items history for the search** — you will be re-alerted for listings already in the database. |
| `enabled` | boolean | `true` | Set to `false` to pause the search without deleting it. Seen-items history is kept, so re-enabling only alerts on listings that appeared while paused. |
| `name` | string | **required** | Human-readable label shown in Pushover notification titles. |
| `query` | string | **required** | eBay keyword search string, exactly as you'd type it in the search bar. |
| `poll_interval_seconds` | integer | `600` | How often to poll eBay. Minimum `60`. High-value or time-sensitive searches may warrant `120`–`300`. |
| `sort` | string | `newly_listed` | Result sort order. See [Sort values](#sort-values). |
| `limit` | integer | `50` | Maximum listings to fetch per poll. eBay Browse API max is `200`. Raise this if you worry about missing items during busy periods. |
| `filters` | object | (see below) | eBay search filters. All sub-fields are optional. |
| `llm` | object | `null` | LLM analysis config. Omit entirely to notify unconditionally on every new listing. |
| `notification` | object | (see below) | Pushover notification settings. |

---

## `filters`

All filter fields are optional. Unset fields produce no filter clause — eBay returns results for all values of that dimension.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `condition` | list of strings | `null` (all) | Item conditions to include. See [Condition values](#condition-values). |
| `buying_options` | list of strings | `null` (all) | Listing types to include. See [Buying option values](#buying-option-values). |
| `price_min` | decimal | `null` | Minimum price in GBP, inclusive. Covers item + postage total. |
| `price_max` | decimal | `null` | Maximum price in GBP, inclusive. Covers item + postage total. |
| `item_location_country` | string | `"GB"` | ISO 3166-1 country code restricting the **seller's** location. `"GB"` limits to UK sellers. |
| `delivery_country` | string | `"GB"` | Restrict to items that can be delivered to this country. Set to `~` (YAML null) to remove the restriction. |
| `category_ids` | list of strings | `null` | eBay UK leaf-category IDs. See [Finding category IDs](#finding-category-ids). |
| `sellers` | list of strings | `null` | Allowlist of seller usernames. Only results from these sellers are returned. |
| `exclude_sellers` | list of strings | `null` | Blocklist of seller usernames. Items from these sellers are hidden. |

### Sort values

| Value | eBay sort | When to use |
|-------|-----------|-------------|
| `newly_listed` | newlyListed | **Recommended for alerts.** Fresh listings appear at the top so the first page always has the newest items. |
| `ending_soonest` | endingSoonest | Auction sniping — shows auctions about to close. |
| `price_asc` | price | Cheapest total price (item + postage) first. |
| `price_desc` | -price | Most expensive first. |
| `best_match` | *(omitted)* | eBay's relevance ranking. Default if no sort is specified. |

### Condition values

| Value | Meaning |
|-------|---------|
| `NEW` | New, unopened, unused |
| `USED` | Standard pre-owned |
| `UNSPECIFIED` | Seller did not specify |
| `CERTIFIED_REFURBISHED` | Manufacturer-certified refurbished |
| `SELLER_REFURBISHED` | Seller refurbished |
| `FOR_PARTS_OR_NOT_WORKING` | As-is / sold for spares |

### Buying option values

| Value | Meaning |
|-------|---------|
| `FIXED_PRICE` | Buy It Now — purchase immediately at the listed price |
| `AUCTION` | Bid-style auction |
| `BEST_OFFER` | Seller accepts offers below the listed price |

### Finding category IDs

Browse to the category on https://www.ebay.co.uk. The ID is the number in the URL path:

```
https://www.ebay.co.uk/b/Camera-Lenses/625/bn_...
                                        ^^^
```

Alternatively, use the eBay Category Tree API (requires your API credentials):

```bash
curl -H "Authorization: Bearer <token>" \
  "https://api.ebay.com/commerce/taxonomy/v1/get_default_category_tree_id?marketplace_id=EBAY_GB"
```

---

## `llm`

When present, each new listing is passed to a local Ollama model along with your `criteria` prompt. The model returns a structured verdict used to decide whether to send a Pushover notification and what to include in it.

Omit the `llm` block entirely to skip analysis and notify on every new listing unconditionally.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | boolean | `true` | Set to `false` to temporarily pause LLM analysis without removing the config. |
| `criteria` | string | **required** | Natural-language instructions for the LLM. See [Writing good criteria](#writing-good-criteria). |
| `min_score_to_notify` | integer 0–10 | `0` | Only send a Pushover notification when the LLM score is ≥ this value. `0` = always notify. `7` = only alert on listings the LLM rates as good buys. |
| `skip_verdicts` | list of strings | `[]` | Suppress notifications for these verdict values even if the score passes. Values: `bid`, `maybe`, `skip`. |
| `model` | string | *(global `OLLAMA_MODEL`)* | Ollama model name for this specific search, overriding the `OLLAMA_MODEL` environment variable. |

### LLM verdict schema

The model is instructed to return JSON with this exact shape:

```json
{
  "recommend": "bid",
  "score": 8,
  "concerns": ["minor paint wear on body"],
  "notes": "Seller describes clean glass and smooth focus. No optical defects mentioned."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `recommend` | `"bid"` \| `"maybe"` \| `"skip"` | Verdict |
| `score` | integer 0–10 | 0 = definite skip, 10 = excellent buy |
| `concerns` | list of strings | Specific issues found in the listing (empty if none) |
| `notes` | string | 1–2 sentence summary |

The Pushover notification title becomes:

```
[BID 8/10] Nikon 50mm f/1.8: Nikkor Ai-S lens — clean glass
```

If Ollama is unavailable or returns unparseable output, the verdict defaults to
`maybe` / score `5` with a `concerns` entry explaining the failure. The notification
is still sent so you don't silently miss a listing.

### Notification suppression

`min_score_to_notify` and `skip_verdicts` work together. Both conditions must pass for a notification to be sent:

```yaml
llm:
  min_score_to_notify: 6     # skip scores 0-5
  skip_verdicts: [skip]      # also suppress the "skip" verdict regardless of score
```

All hits — including suppressed ones — are written to `data/state.sqlite` with `notified = 0`. Query them any time:

```bash
sqlite3 data/state.sqlite \
  "SELECT title, verdict, score, notified FROM hits ORDER BY created_at DESC LIMIT 20;"
```

### Writing good criteria

The criteria field is the most important part of the config. Be specific:

- **Name the item type** so the model understands the domain.
- **List concrete positive signals** (what makes a good buy).
- **List concrete negative keywords** to flag (copy-paste terms buyers actually use).
- **Clarify trade-offs** (e.g. cosmetic wear is fine, optical defects are not).

**Example — vintage camera lens:**

```yaml
llm:
  criteria: |
    This is a vintage manual-focus Nikon Ai-S camera lens.
    Recommend BID only if the listing strongly suggests all of the following:
      - Optics are clean: no fungus, haze, fogging, or internal cleaning marks
      - No scratches, chips, or coating damage on front or rear elements
      - Smooth focus ring — no stiffness, grinding, or play mentioned
      - Aperture blades are clean (not oily) and snappy
      - Physical body in decent shape (minor cosmetic marks are fine)
    Recommend SKIP if any of these keywords appear:
      as-is, for parts, spares or repair, untested, haze, fungus, fog,
      scratches, stiff focus, oily blades, separation, internal marks,
      cleaning marks, element damage
    Be lenient on cosmetic body wear but strict on optical quality.
```

**Example — vintage hi-fi amplifier:**

```yaml
llm:
  criteria: |
    This is a vintage hi-fi integrated amplifier.
    Recommend BID if:
      - Powers on and plays audio (both channels working)
      - No obvious burnt components or transformer hum mentioned
      - Seller has tested it, even briefly
    Recommend SKIP if:
      - Not tested / untested / as-is
      - Blown fuse, no power, dead channel, burnt smell mentioned
      - Missing knobs or fascia damage unless price reflects it
```

### Vision model support

Image URLs for each listing are always included in the payload sent to Ollama. Text-only models ignore them. To enable image-based analysis, set a vision-capable model in the `llm.model` field:

```yaml
llm:
  model: llava          # or qwen2-vl, llava-phi3, moondream
  criteria: |
    Examine the photos closely. Flag any visible fungus (white web-like
    patches inside the lens), haze (milky internal fogging), or scratches
    on the front or rear elements. Also check for oily aperture blades.
```

Pull the model first: `ollama pull llava`

---

## `notification`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pushover_priority` | integer −2 to 2 | `0` | See priority table below. |
| `pushover_sound` | string | *(device default)* | Pushover sound name. See sound table below. |

### Priority values

| Value | Behaviour |
|-------|-----------|
| `−2` | No notification delivered at all. The hit is stored in SQLite only. |
| `−1` | Quiet — always delivered but no sound or vibration. |
| `0` | Normal. |
| `1` | High priority — bypasses device quiet hours. |
| `2` | Emergency — repeats every 30 s until acknowledged. Use sparingly. |

Use `1` or `2` for searches sorted by `ending_soonest` where timing matters.

### Sound values

`pushover, bike, bugle, cashregister, classical, cosmic, falling, gamelan,
incoming, intermission, magic, mechanical, pianobar, siren, spacealarm,
tugboat, alien, climb, persistent, echo, updown, vibrate, none`

---

## Full annotated example

```yaml
- id: nikon-50mm-ais             # stable slug — don't change without resetting history
  name: "Nikon 50mm f/1.8 Ai-S" # shown in Pushover title
  query: "nikon 50mm 1.8 ais nikkor"
  poll_interval_seconds: 300     # check every 5 minutes
  sort: newly_listed
  limit: 50

  filters:
    condition: [USED, SELLER_REFURBISHED]
    buying_options: [FIXED_PRICE, AUCTION]
    price_min: 30
    price_max: 150
    item_location_country: GB    # UK sellers only
    delivery_country: GB         # must ship to UK

  llm:
    criteria: |
      This is a vintage manual-focus Nikon Ai-S lens.
      BID if optics are clean with no fungus, haze, or scratches.
      SKIP if: as-is, for parts, untested, haze, fungus, scratches, stiff focus.
    min_score_to_notify: 5       # suppress low-confidence hits
    skip_verdicts: [skip]        # never notify on "skip" verdict

  notification:
    pushover_priority: 0
    pushover_sound: cosmic
```
