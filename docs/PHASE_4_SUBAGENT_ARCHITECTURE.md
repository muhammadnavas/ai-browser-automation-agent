# Phase 4 — Subagent orchestration

## Implemented

- Task-scoped child IDs, isolated input and private memory.
- Role registry for navigation, research, form and verification workers.
- Bounded concurrency and deterministic result aggregation in request order.
- Per-worker timeouts, explicit failure states, timestamps and durations.
- Parent cancellation and fail-fast sibling cancellation.
- Dedicated Chrome Extension tab per browser worker, with extension-side tab cleanup.
- Form worker fills only explicitly mapped, editable fields; it never submits and leaves the tab unlocked for human review.
- URL validation runs before child-tab navigation.
- TaskManager integration and API endpoints to run/read child batches.

The normal task-creation endpoint can also ask the master task to run child
workers before its existing browser execution loop. Add `subagent_specs` to
the `POST /api/tasks` body; omitting it preserves current behavior:

```json
{
  "goal": "Research this page and verify the key details",
  "start_url": "https://example.com",
  "subagent_specs": [
    {
      "role": "research",
      "goal": "Collect the page's public details",
      "input": {"url": "https://example.com"}
    },
    {
      "role": "verification",
      "goal": "Check that the expected title is visible",
      "input": {"url": "https://example.com", "expected_text": "Example Domain"}
    }
  ]
}
```

The master task stores bounded child outputs in its task context so the existing
planner can use the delegated evidence during the main execution.

## Worker responsibilities

| Role | Responsibility | Safety boundary |
|---|---|---|
| navigation | Open URL and inspect live DOM | Own child tab |
| research | Extract bounded visible text, headings and links | Read-only |
| form | Fill explicitly supplied values | Skips password/file/hidden fields; never submits; retains tab for human review |
| verification | Check URL and visible-text postconditions | Reports evidence; does not claim unobserved success |

## API

Run a batch under an existing parent task:

```http
POST /api/tasks/{task_id}/subagents?fail_fast=true
Content-Type: application/json
```

```json
[
  {"role":"research","goal":"Collect public page information","input":{"url":"https://example.com"}},
  {"role":"verification","goal":"Verify the expected page","input":{"url":"https://example.com","expected_text":"Example Domain"}}
]
```

Read the latest aggregated child results:

```http
GET /api/tasks/{task_id}/subagents
```

Close a retained form-review tab after review:

```http
POST /api/tasks/{task_id}/subagents/{subagent_id}/close-tab
```

## Browser isolation

Each browser worker creates a unique task-owned tab in the connected Chrome
session. The extension maps commands to the owning task ID, so workers do not
share a mutable tab. Navigation, research and verification workers close their exact task tabs in cleanup. The form worker intentionally keeps its populated tab open and unlocked for human review; the close-tab endpoint releases it, and cancelling the parent closes retained review tabs.
Workers that intentionally target a shared resource must still request an
exclusive resource lock with a stable resource_key. Cancelling the parent task
also closes any retained form-review tabs.

## Remaining production validation

The new tests cover orchestration, isolation contracts, worker behavior and
safe form handling. They still need to be run in the project's backend
environment, followed by an end-to-end test with the actual Chrome Extension.
The existing default single-agent task loop remains intact; the new subagent
batch API is an explicit delegation interface rather than an automatic rewrite
of every task into parallel workers.
