is_valid_email rejects real addresses

`is_valid_email` rejects addresses that are perfectly valid:

- dots and plus signs in the local part: `first.last@example.com`, `user+tag@example.com`
- uppercase letters: `USER@EXAMPLE.COM`
- multi-part domains: `a@mail.example.co.uk`

It must keep rejecting clearly invalid input, such as a missing `@`, no dot in the domain, spaces, an empty local part, an empty domain label like `a@.com`, or a one-letter top-level domain like `a@example.c`.
