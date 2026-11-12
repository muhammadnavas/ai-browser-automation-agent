# Automata multi-agent orchestration

## Lifecycle

1. **Master planning** — the configured planner LLM receives the original user goal and known starting URL. It may select up to three bounded read-only preflight subtasks.
2. **Specialist execution** — Navigation and Research workers run through the existing Chrome Extension bridge in child-owned tabs. The orchestrator limits concurrency, enforces timeouts, supports cancellation and aggregates results.
3. **Master follow-up** — the master reviews bounded result summaries and may request one additional batch of up to two targeted read-only workers. Completed work is not repeated; a failed/timed-out role may be retried. This is deliberately not an unbounded agent loop.
4. **Main execution** — specialist output is added to parent task context. The existing main planner remains the sole authority for browser actions. Action policy, URL validation, approval gates and task cancellation continue to apply.
5. **Recovery** — after a recoverable action failure, the Recovery Agent receives a sanitized snapshot and error. It can diagnose and suggest up to eight milestones; it cannot click, type, navigate or submit. The parent planner executes subsequent actions. The established adaptive replan method is used if the specialist fails.
6. **Verification** — the primary task verifier must first approve FINISH. A second Verification Agent then independently judges the current page on the same task tab. An explicit negative result prevents completion. If the second verifier is unavailable, the primary verifier remains authoritative and the secondary check is marked unavailable.

## Registered roles

| Role | Purpose | Browser access |
|---|---|---|
| navigation | Inspect URL, title, headings and interactive element count | Dedicated read-only child tab |
| research | Collect bounded visible text, headings and links | Dedicated read-only child tab |
| form | Fill explicitly mapped visible fields; never submit | Dedicated child tab retained for human review |
| verification | Independently evaluate final goal evidence, or check explicit expected text/URL | Read-only parent tab when verifying a completed task; dedicated tab for explicit checks |
| recovery | Diagnose a failed action and suggest revised milestones | No browser access |

## Safety boundaries

- Automatic master delegation may choose only navigation and research.
- The master can use only the exact known starting URL for automatic preflight work.
- Mutating user goals are excluded from automatic preflight delegation.
- The follow-up round is limited to one batch and two workers.
- Recovery returns milestones, not executable browser actions.
- The main browser planner and existing deterministic action-policy layer remain responsible for all actions.
- The verification specialist cannot modify the page.
- Child workers inherit only explicitly passed parent memory; sensitive form values and arbitrary page attributes are not copied into recovery snapshots.
- Parent cancellation propagates to active child tasks and retained form-review tabs.

## Diagnostics

Task plan_metadata records:
- delegation: automatic/explicit mode and selected roles.
- subagent_batch_status, subagent_summary, and subagent_results_count.
- followup_delegation: whether the master requested a second read-only batch.
- recovery_agent: recovery status, diagnosis, strategy and confidence.
- independent_verification: secondary verifier status, result and evidence.

Individual specialist outputs are also appended to bounded context.extracted_snippets with their role, status, goal and result.

## Operational limits

This architecture is hierarchical but not a general-purpose swarm. Workers are bounded specialists, not unrestricted autonomous browser sessions. Automatic preflight requires a known starting URL and a connected Chrome Extension. Research is limited to visible content from the loaded page; it does not crawl arbitrary websites. Verification can be inconclusive on pages where completion evidence is transient or hidden. Provider availability, page authentication and site-specific DOM behavior still affect results.