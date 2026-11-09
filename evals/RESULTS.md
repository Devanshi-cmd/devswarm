# DevSwarm evaluation

12 tasks. A task counts as solved only if the system finished and the task's hidden tests pass.

## Solve rate

| Configuration | Solved | Solve rate | Avg iterations | Tokens | Cost | Time |
|---|---|---|---|---|---|---|
| devswarm-haiku | 12/12 | 100% | 1.2 | 740,018 | $0.0615 | 516s |
| devswarm-haiku-no-reviewer | 12/12 | 100% | 1.0 | 537,267 | $0.0429 | 387s |
| claude-code-sonnet | 12/12 | 100% | - | 479,495 | $0.4709 | 107s |

## By task

| Task | devswarm-haiku | devswarm-haiku-no-reviewer | claude-code-sonnet |
|---|---|---|---|
| bad_email_regex | solved | solved | solved |
| checkout_discount_tax | solved | solved | solved |
| csv_quoted_fields | solved | solved | solved |
| deep_merge_config | solved | solved | solved |
| empty_input_statistics | solved | solved | solved |
| leap_year_centuries | solved | solved | solved |
| lru_cache_recency | solved | solved | solved |
| merge_intervals | solved | solved | solved |
| off_by_one_pagination | solved | solved | solved |
| retry_attempts | solved | solved | solved |
| shared_cart_items | solved | solved | solved |
| slugify_max_length | solved | solved | solved |

## Details

| Task | Configuration | Result | Iterations | Tokens | Cost | Time | Note |
|---|---|---|---|---|---|---|---|
| bad_email_regex | devswarm-haiku | solved | 1 | 69,874 | $0.0053 | 47s |  |
| bad_email_regex | devswarm-haiku-no-reviewer | solved | 1 | 53,510 | $0.0042 | 36s |  |
| bad_email_regex | claude-code-sonnet | solved | - | 57,390 | $0.0463 | 12s |  |
| checkout_discount_tax | devswarm-haiku | solved | 1 | 53,655 | $0.0046 | 37s |  |
| checkout_discount_tax | devswarm-haiku-no-reviewer | solved | 1 | 51,079 | $0.0037 | 38s |  |
| checkout_discount_tax | claude-code-sonnet | solved | - | 32,841 | $0.0390 | 8s |  |
| csv_quoted_fields | devswarm-haiku | solved | 1 | 55,766 | $0.0059 | 47s |  |
| csv_quoted_fields | devswarm-haiku-no-reviewer | solved | 1 | 54,989 | $0.0058 | 47s |  |
| csv_quoted_fields | claude-code-sonnet | solved | - | 41,575 | $0.0424 | 10s |  |
| deep_merge_config | devswarm-haiku | solved | 4 | 182,795 | $0.0150 | 114s |  |
| deep_merge_config | devswarm-haiku-no-reviewer | solved | 1 | 52,156 | $0.0041 | 33s |  |
| deep_merge_config | claude-code-sonnet | solved | - | 43,896 | $0.0393 | 10s |  |
| empty_input_statistics | devswarm-haiku | solved | 1 | 35,861 | $0.0028 | 27s |  |
| empty_input_statistics | devswarm-haiku-no-reviewer | solved | 1 | 36,286 | $0.0023 | 28s |  |
| empty_input_statistics | claude-code-sonnet | solved | - | 43,947 | $0.0404 | 9s |  |
| leap_year_centuries | devswarm-haiku | solved | 1 | 41,180 | $0.0030 | 29s |  |
| leap_year_centuries | devswarm-haiku-no-reviewer | solved | 1 | 36,965 | $0.0023 | 23s |  |
| leap_year_centuries | claude-code-sonnet | solved | - | 31,723 | $0.0352 | 7s |  |
| lru_cache_recency | devswarm-haiku | solved | 1 | 69,895 | $0.0052 | 43s |  |
| lru_cache_recency | devswarm-haiku-no-reviewer | solved | 1 | 36,360 | $0.0029 | 26s |  |
| lru_cache_recency | claude-code-sonnet | solved | - | 44,122 | $0.0399 | 9s |  |
| merge_intervals | devswarm-haiku | solved | 1 | 40,558 | $0.0035 | 30s |  |
| merge_intervals | devswarm-haiku-no-reviewer | solved | 1 | 35,995 | $0.0028 | 25s |  |
| merge_intervals | claude-code-sonnet | solved | - | 32,521 | $0.0387 | 9s |  |
| off_by_one_pagination | devswarm-haiku | solved | 1 | 39,646 | $0.0029 | 30s |  |
| off_by_one_pagination | devswarm-haiku-no-reviewer | solved | 1 | 30,206 | $0.0020 | 22s |  |
| off_by_one_pagination | claude-code-sonnet | solved | - | 31,632 | $0.0341 | 7s |  |
| retry_attempts | devswarm-haiku | solved | 1 | 53,395 | $0.0043 | 34s |  |
| retry_attempts | devswarm-haiku-no-reviewer | solved | 1 | 47,346 | $0.0034 | 32s |  |
| retry_attempts | claude-code-sonnet | solved | - | 43,586 | $0.0378 | 8s |  |
| shared_cart_items | devswarm-haiku | solved | 1 | 39,861 | $0.0028 | 31s |  |
| shared_cart_items | devswarm-haiku-no-reviewer | solved | 1 | 31,882 | $0.0023 | 24s |  |
| shared_cart_items | claude-code-sonnet | solved | - | 31,902 | $0.0356 | 8s |  |
| slugify_max_length | devswarm-haiku | solved | 1 | 57,532 | $0.0062 | 46s |  |
| slugify_max_length | devswarm-haiku-no-reviewer | solved | 1 | 70,493 | $0.0071 | 56s |  |
| slugify_max_length | claude-code-sonnet | solved | - | 44,360 | $0.0421 | 11s |  |

## Configurations

- **devswarm-haiku:** DevSwarm on Haiku, with the Reviewer
- **devswarm-haiku-no-reviewer:** DevSwarm on Haiku, without the Reviewer
- **claude-code-sonnet:** Headless Claude Code on Sonnet, alone

Claude Code's cost is the figure it reports for itself. It runs on a Claude subscription login here, so that amount is notional and was not billed.
