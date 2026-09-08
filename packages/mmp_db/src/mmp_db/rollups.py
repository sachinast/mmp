"""Pre-aggregated reporting tables.

On this stack rollups are not an optimisation, they are **the read path**. A
dashboard that queries raw events is fine at ten million rows and unusable at a
billion, and the transition happens without warning on whichever advertiser
grows fastest. So every dashboard screen reads a rollup, and the raw partitions
are reserved for the event explorer, which always carries a bounded date range.

**Cardinality is the design constraint.** A rollup keyed by every available
dimension is not a rollup — cross ``event_name`` with campaign, platform,
country and hour and the aggregate can approach the size of the source. So the
dimensions are split by what each question actually needs:

* ``rollup_events_hourly`` — volume and revenue by event and platform. Hourly,
  because operational questions ("did ingestion stop?") need recent resolution.
  No campaign dimension: attributing an event to a campaign requires a join, and
  paying for that join on every event would defeat the purpose.
* ``rollup_clicks_hourly`` — click volume by campaign. Cheap; clicks already
  carry their campaign.
* ``rollup_campaign_daily`` — the campaign performance table, which does pay for
  the attribution join. Daily, not hourly, because that is the granularity the
  join can afford and the granularity advertisers actually report on.

Every rollup is **recomputed** over a bounded window rather than incremented.
An idempotent recompute is safe to run twice, safe after a worker crash, and
safe to re-run for a window that received late data. An incrementing counter is
none of those, and a counter that double-counts after a restart becomes an
invoice dispute.

**Distinct counts do not aggregate.** ``unique_devices`` is exact *within its
own hour* and means nothing when summed or maxed across hours: summing
double-counts every device active in two hours, and taking the maximum reports
the busiest single hour. There is no arithmetic that recovers a period-level
distinct count from per-bucket distinct counts — that needs either a scan of the
raw events or a mergeable sketch (HyperLogLog), and neither belongs in the
default read path. So the analytics API exposes this as *peak hourly devices*
and says so. Reporting it as "unique devices for the week" would be a number
that is confidently, silently wrong, which is the worst thing a measurement
product can produce.

**Buckets are keyed on ``occurred_at``, scanned by ``received_at``.** That
distinction is the subtlest thing in this module and it is worth stating plainly.

An advertiser asking for "installs on Tuesday" means installs that *happened* on
Tuesday. Bucketing on arrival time would report a device that spent a week
offline as a spike today for activity that happened last week — the numbers
would be defensible and useless. So the group-by is ``occurred_at``.

But the tables are partitioned on ``received_at``, and that is also what bounds a
scan. So each refresh scans an arrival window and groups by occurrence, with one
guard: it writes only buckets its scan window covers *completely*
(``occurred_at >= window_start``). Without that guard a trailing refresh would
see one late event belonging to an old bucket, recompute that bucket from that
single event, and overwrite the real count with 1. Older buckets are left to the
nightly late-arrival pass, whose scan window is wide enough to cover them in
full.
"""

from __future__ import annotations

CREATE_EVENTS_HOURLY = """
CREATE TABLE IF NOT EXISTS rollup_events_hourly (
    organization_id uuid        NOT NULL,
    app_id          uuid        NOT NULL,
    bucket_hour     timestamptz NOT NULL,
    event_name      text        NOT NULL,
    platform        smallint    NOT NULL DEFAULT 0,
    event_count     bigint      NOT NULL DEFAULT 0,
    -- Distinct devices *within this hour*. Deliberately not summable or
    -- maxable into a period total: see the note on distinct counts below.
    unique_devices  bigint      NOT NULL DEFAULT 0,
    revenue_minor   bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, bucket_hour, event_name, platform)
)
"""

CREATE_CLICKS_HOURLY = """
CREATE TABLE IF NOT EXISTS rollup_clicks_hourly (
    organization_id uuid        NOT NULL,
    app_id          uuid        NOT NULL,
    bucket_hour     timestamptz NOT NULL,
    -- NOT NULL with a nil-UUID sentinel rather than a nullable column.
    -- A primary key cannot contain NULL, and more importantly NULLs do not
    -- compare equal: with a nullable campaign_id the ON CONFLICT clause below
    -- would never match, so every refresh would insert a duplicate row for
    -- unattached clicks instead of updating the existing one.
    campaign_id     uuid        NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    platform        smallint    NOT NULL DEFAULT 0,
    click_count     bigint      NOT NULL DEFAULT 0,
    bot_count       bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, bucket_hour, campaign_id, platform)
)
"""

CREATE_CAMPAIGN_DAILY = """
CREATE TABLE IF NOT EXISTS rollup_campaign_daily (
    organization_id uuid        NOT NULL,
    app_id          uuid        NOT NULL,
    bucket_day      date        NOT NULL,
    campaign_id     uuid        NOT NULL,
    clicks          bigint      NOT NULL DEFAULT 0,
    installs        bigint      NOT NULL DEFAULT 0,
    organic_installs bigint     NOT NULL DEFAULT 0,
    revenue_minor   bigint      NOT NULL DEFAULT 0,
    conversions     bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, bucket_day, campaign_id)
)
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_rollup_events_hourly_org "
    "ON rollup_events_hourly (organization_id, bucket_hour)",
    "CREATE INDEX IF NOT EXISTS ix_rollup_clicks_hourly_org "
    "ON rollup_clicks_hourly (organization_id, bucket_hour)",
    "CREATE INDEX IF NOT EXISTS ix_rollup_campaign_daily_org "
    "ON rollup_campaign_daily (organization_id, bucket_day)",
)

TABLES = ("rollup_events_hourly", "rollup_clicks_hourly", "rollup_campaign_daily")

# "No campaign" — an organic or unattached click. A sentinel rather than NULL so
# it can sit in a primary key and compare equal to itself in ON CONFLICT.
NO_CAMPAIGN = "00000000-0000-0000-0000-000000000000"

CREATE_ALL = (CREATE_EVENTS_HOURLY, CREATE_CLICKS_HOURLY, CREATE_CAMPAIGN_DAILY, *INDEXES)

# --- refresh statements -------------------------------------------------
#
# Each takes a [start, end) window and recomputes it wholesale. ON CONFLICT DO
# UPDATE with the recomputed value (not an increment) is what makes a re-run a
# no-op rather than a doubling.

REFRESH_EVENTS_HOURLY = """
INSERT INTO rollup_events_hourly (
    organization_id, app_id, bucket_hour, event_name, platform,
    event_count, unique_devices, revenue_minor, updated_at
)
SELECT
    organization_id,
    app_id,
    date_trunc('hour', occurred_at) AS bucket_hour,
    event_name,
    coalesce(platform, 0) AS platform,
    count(*) AS event_count,
    count(DISTINCT anonymous_id) AS unique_devices,
    coalesce(sum(revenue_minor), 0) AS revenue_minor,
    now()
FROM events
-- Scanned by received_at, which is the partition key and bounds the work.
-- Grouped by occurred_at, which is what a customer means by "on Tuesday".
-- The occurred_at floor keeps us from writing a bucket this scan cannot see in
-- full: without it, one late event would overwrite an old bucket's real count.
WHERE received_at >= $1 AND received_at < $2 AND occurred_at >= $1
GROUP BY organization_id, app_id, date_trunc('hour', occurred_at), event_name,
         coalesce(platform, 0)
ON CONFLICT (app_id, bucket_hour, event_name, platform) DO UPDATE SET
    event_count = EXCLUDED.event_count,
    unique_devices = EXCLUDED.unique_devices,
    revenue_minor = EXCLUDED.revenue_minor,
    updated_at = now()
"""

REFRESH_CLICKS_HOURLY = """
INSERT INTO rollup_clicks_hourly (
    organization_id, app_id, bucket_hour, campaign_id, platform,
    click_count, bot_count, updated_at
)
SELECT
    organization_id,
    app_id,
    date_trunc('hour', clicked_at) AS bucket_hour,
    coalesce(campaign_id, '00000000-0000-0000-0000-000000000000'::uuid) AS campaign_id,
    coalesce(platform, 0) AS platform,
    count(*) AS click_count,
    count(*) FILTER (WHERE is_bot) AS bot_count,
    now()
FROM clicks
WHERE clicked_at >= $1 AND clicked_at < $2
GROUP BY organization_id, app_id, date_trunc('hour', clicked_at),
         coalesce(campaign_id, '00000000-0000-0000-0000-000000000000'::uuid),
         coalesce(platform, 0)
ON CONFLICT (app_id, bucket_hour, campaign_id, platform) DO UPDATE SET
    click_count = EXCLUDED.click_count,
    bot_count = EXCLUDED.bot_count,
    updated_at = now()
"""

# The expensive one, and the reason it is daily.
#
# Revenue has to be attributed, which means joining events to attributions on
# (app_id, anonymous_id). That join is why campaign reporting cannot be hourly
# on this stack, and it is the query to watch as volume grows: when it stops
# fitting in the refresh interval, that is the signal to add a columnar sink,
# not a signal to add an index.
REFRESH_CAMPAIGN_DAILY = """
WITH day_clicks AS (
    SELECT organization_id, app_id, campaign_id,
           date_trunc('day', clicked_at)::date AS bucket_day,
           count(*) AS clicks
    FROM clicks
    WHERE clicked_at >= $1 AND clicked_at < $2 AND campaign_id IS NOT NULL
      AND NOT is_bot
    GROUP BY 1, 2, 3, 4
),
day_installs AS (
    SELECT organization_id, app_id, campaign_id,
           date_trunc('day', installed_at)::date AS bucket_day,
           count(*) AS installs
    FROM attributions
    WHERE installed_at >= $1 AND installed_at < $2
      AND superseded_by IS NULL AND campaign_id IS NOT NULL
    GROUP BY 1, 2, 3, 4
),
day_revenue AS (
    SELECT a.organization_id, a.app_id, a.campaign_id,
           date_trunc('day', e.occurred_at)::date AS bucket_day,
           coalesce(sum(e.revenue_minor), 0) AS revenue_minor,
           count(*) FILTER (WHERE e.revenue_minor IS NOT NULL) AS conversions
    FROM events e
    JOIN attributions a
      ON a.app_id = e.app_id
     AND a.anonymous_id = e.anonymous_id
     AND a.superseded_by IS NULL
     AND a.campaign_id IS NOT NULL
     AND e.received_at <= a.expires_at
    WHERE e.received_at >= $1 AND e.received_at < $2 AND e.occurred_at >= $1
    GROUP BY 1, 2, 3, 4
)
INSERT INTO rollup_campaign_daily (
    organization_id, app_id, bucket_day, campaign_id,
    clicks, installs, organic_installs, revenue_minor, conversions, updated_at
)
SELECT
    coalesce(c.organization_id, i.organization_id, r.organization_id),
    coalesce(c.app_id, i.app_id, r.app_id),
    coalesce(c.bucket_day, i.bucket_day, r.bucket_day),
    coalesce(c.campaign_id, i.campaign_id, r.campaign_id),
    coalesce(c.clicks, 0),
    coalesce(i.installs, 0),
    0,
    coalesce(r.revenue_minor, 0),
    coalesce(r.conversions, 0),
    now()
FROM day_clicks c
FULL OUTER JOIN day_installs i
  ON i.app_id = c.app_id AND i.campaign_id = c.campaign_id
 AND i.bucket_day = c.bucket_day
FULL OUTER JOIN day_revenue r
  ON r.app_id = coalesce(c.app_id, i.app_id)
 AND r.campaign_id = coalesce(c.campaign_id, i.campaign_id)
 AND r.bucket_day = coalesce(c.bucket_day, i.bucket_day)
ON CONFLICT (app_id, bucket_day, campaign_id) DO UPDATE SET
    clicks = EXCLUDED.clicks,
    installs = EXCLUDED.installs,
    revenue_minor = EXCLUDED.revenue_minor,
    conversions = EXCLUDED.conversions,
    updated_at = now()
"""
