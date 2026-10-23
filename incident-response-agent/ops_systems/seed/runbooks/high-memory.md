---
alert: HighMemoryUsage
title: Service memory usage is high
owner: platform-oncall
---

# HighMemoryUsage

The service's memory usage is above 90% for 15 minutes.

## Investigate

1. Post an acknowledgement in the incident channel.
2. Query `memory` for the last 30 minutes. A steady climb points to a leak.
3. Search the service's ERROR logs for out-of-memory kills.
4. List the service's deploys of the last 24 hours.

## Mitigate

- Restart the service (`restart_service`) to reclaim memory. Needs approval.
- If a deploy in the last 24 hours introduced the climb, also open a follow-up to roll it back.

## Verify

- Query `memory` again and post the result in the incident channel.

## Close

- Write the incident summary with the timeline, the root cause and the follow-ups.
