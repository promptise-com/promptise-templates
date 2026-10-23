---
alert: HighErrorRate
title: Service 5xx error rate is high
owner: platform-oncall
---

# HighErrorRate

The service answers more than 5% of requests with a 5xx status for 5 minutes.

## Investigate

1. Post an acknowledgement in the incident channel.
2. Search the service's ERROR logs for the last 30 minutes. Note the most frequent error.
3. Query `error_rate` and `latency_p95_ms` for the last 30 minutes. Note when the rise started.
4. List the service's deploys of the last 24 hours. If one finished shortly before the rise, search the
   service's INFO logs from that minute for config changes.
5. Check the error rate of the dependency named in the errors. If the dependency is unhealthy, page its team instead of changing this service.

## Mitigate

- If a deploy finished within 20 minutes before the errors started, roll the service back to the
  previous version (`rollback_deploy`). Needs approval from the on-call engineer.
- If there is no recent deploy and the errors are resource exhaustion (out of memory, connection
  pool full), restart the service (`restart_service`). Needs approval.
- Never scale down a service during this alert.

## Verify

- Query `error_rate` again. The incident is mitigated when the current value is below 1%.
- Post the result in the incident channel.

## Close

- Write the incident summary with the timeline, the root cause and the follow-ups.
