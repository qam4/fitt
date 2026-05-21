---
name: fitt-status
description: Report FITT's runtime status by gathering live tool/state info.
prerequisites: []
---

# FITT Status

When the user asks for FITT's status, run these tools in order:

1. `list_capabilities` — returns the live tool list with descriptions.
2. `cron_list` — returns scheduled cron jobs (may be empty).
3. `learn_list` — returns learned corrections.

Then format the result as:

```
🟢 FITT [PHASE-4.10] is alive.
   Tools: <count> registered
   Crons: <count> scheduled
   Lessons: <count> learned corrections
```

Use the literal counts from the tool calls. Do not invent
numbers. If a tool returns an empty list, the count is `0`.

Do NOT use `http_get` to query the gateway over the network —
the gateway tools live in the same process and return state
directly. A self-loopback HTTP call from inside the gateway
fails with `transport error`.
