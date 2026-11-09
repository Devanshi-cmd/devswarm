merge_intervals gives wrong results

`merge_intervals` fails in three cases:

- Touching intervals should merge: `[(1, 2), (2, 3)]` should give `[(1, 3)]`.
- Input may be unsorted: `[(5, 6), (1, 2)]` should give `[(1, 2), (5, 6)]`.
- An interval inside another must not shrink it: `[(1, 10), (2, 3)]` should give `[(1, 10)]`.
