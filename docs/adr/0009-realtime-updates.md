# 9. Real-time updates and alerts

Date: 2026-10-02 · Status: accepted

## Decision
1. The importer commits new prices and curves to Postgres in one transaction.
2. It then evaluates alert rules and records fired alerts with unique event IDs.
3. After commit, it publishes `dataset_updated` and `alert_fired` to Redis Pub/Sub.
4. The API relays these events to browsers over server-sent events.
5. Redis Pub/Sub is at-most-once, so on reconnect the browser re-fetches the current dataset version over HTTP and de-duplicates events by ID.
6. Cached responses are keyed by dataset version.
7. If Redis is down, reads fall back to Postgres.
8. Data changes once a day, so a replay CLI steps through historical dates to demonstrate live updates.

## Consequences
- An event can be lost but state cannot. Postgres is the source of truth.
- The outbox pattern is deliberately skipped, because re-fetching on reconnect covers missed events at this scale.
