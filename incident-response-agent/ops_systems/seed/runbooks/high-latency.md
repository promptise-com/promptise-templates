---
alert: HighLatency
title: Service p95 latency is high
owner: platform-oncall
---

# HighLatency

The service's p95 latency is above its objective for 10 minutes.

## Investigate

1. Post an acknowledgement in the incident channel.
2. Query `latency_p95_ms`, `cpu` and `request_rate` for the last 30 minutes.
3. Search the service's WARN and ERROR logs for slow downstream calls.
4. List the service's deploys of the last 24 hours.

## Mitigate

- If CPU is above 85% and the request rate rose, scale the service up by 50% (`scale_service`).
  Needs approval.
- If a deploy finished within 20 minutes before the rise, roll back (`rollback_deploy`). Needs approval.

## Verify

- Query `latency_p95_ms` again and post the result in the incident channel.

## Close

- Write the incident summary with the timeline, the root cause and the follow-ups.
