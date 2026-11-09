split() loses or invents cents

`split(amount_cents, weights)` divides an integer number of cents between people according to integer weights, but the shares often don't add up to the amount. For example `split(100, [1, 1, 1])` returns a total of 99.

Required behaviour:

- The shares must always add up to exactly `amount_cents`.
- Each share starts as the rounded-down proportional amount. Leftover cents go one at a time to the shares with the largest fractional remainders. If remainders tie, the earlier position gets the cent first.
- A person with weight 0 always gets 0.
- For a negative amount, work out the split of the positive amount and negate each share, so `split(-100, [1, 1, 1])` is `[-34, -33, -33]`.
- If the weights don't add up to more than 0, raise ValueError.
