# DevSwarm evaluation

6 tasks. A task counts as solved only if the system finished and the task's hidden tests pass.

## Solve rate

| Configuration | Solved | Solve rate | Avg iterations | Tokens | Cost | Time |
|---|---|---|---|---|---|---|
| devswarm-haiku | 6/6 | 100% | 1.3 | 715,012 | $0.0670 | 619s |
| devswarm-haiku-no-reviewer | 6/6 | 100% | 1.2 | 467,021 | $0.0428 | 321s |
| claude-code-sonnet | 6/6 | 100% | - | 279,020 | $0.2812 | 78s |

## By task

| Task | devswarm-haiku | devswarm-haiku-no-reviewer | claude-code-sonnet |
|---|---|---|---|
| hard_bill_split | solved | solved | solved |
| hard_event_bus | solved | solved | solved |
| hard_inventory_orders | solved | solved | solved |
| hard_path_glob | solved | solved | solved |
| hard_task_scheduler | solved | solved | solved |
| hard_template_engine | solved | solved | solved |

## Details

| Task | Configuration | Result | Iterations | Tokens | Cost | Time | Note |
|---|---|---|---|---|---|---|---|
| hard_bill_split | devswarm-haiku | solved | 1 | 59,365 | $0.0071 | 50s |  |
| hard_bill_split | devswarm-haiku-no-reviewer | solved | 1 | 54,620 | $0.0053 | 43s |  |
| hard_bill_split | claude-code-sonnet | solved | - | 47,088 | $0.0302 | 11s |  |
| hard_event_bus | devswarm-haiku | solved | 1 | 93,168 | $0.0078 | 59s |  |
| hard_event_bus | devswarm-haiku-no-reviewer | solved | 1 | 68,099 | $0.0061 | 47s |  |
| hard_event_bus | claude-code-sonnet | solved | - | 47,610 | $0.0566 | 15s |  |
| hard_inventory_orders | devswarm-haiku | solved | 1 | 79,384 | $0.0078 | 53s |  |
| hard_inventory_orders | devswarm-haiku-no-reviewer | solved | 1 | 59,908 | $0.0053 | 38s |  |
| hard_inventory_orders | claude-code-sonnet | solved | - | 46,040 | $0.0465 | 12s |  |
| hard_path_glob | devswarm-haiku | solved | 3 | 311,228 | $0.0268 | 329s |  |
| hard_path_glob | devswarm-haiku-no-reviewer | solved | 2 | 160,595 | $0.0150 | 108s |  |
| hard_path_glob | claude-code-sonnet | solved | - | 46,421 | $0.0529 | 16s |  |
| hard_task_scheduler | devswarm-haiku | solved | 1 | 78,779 | $0.0086 | 61s |  |
| hard_task_scheduler | devswarm-haiku-no-reviewer | solved | 1 | 61,410 | $0.0055 | 41s |  |
| hard_task_scheduler | claude-code-sonnet | solved | - | 45,863 | $0.0475 | 12s |  |
| hard_template_engine | devswarm-haiku | solved | 1 | 93,088 | $0.0089 | 67s |  |
| hard_template_engine | devswarm-haiku-no-reviewer | solved | 1 | 62,389 | $0.0056 | 44s |  |
| hard_template_engine | claude-code-sonnet | solved | - | 45,998 | $0.0475 | 13s |  |

## Configurations

- **devswarm-haiku:** DevSwarm on Haiku, with the Reviewer
- **devswarm-haiku-no-reviewer:** DevSwarm on Haiku, without the Reviewer
- **claude-code-sonnet:** Headless Claude Code on Sonnet, alone

Claude Code's cost is the figure it reports for itself. It runs on a Claude subscription login here, so that amount is notional and was not billed.
