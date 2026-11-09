Century years are treated as leap years

`is_leap(1900)` returns True, but 1900 was not a leap year. Years divisible by 100 are only leap years if they are also divisible by 400. This also makes `days_in_month(1900, 2)` wrong.
