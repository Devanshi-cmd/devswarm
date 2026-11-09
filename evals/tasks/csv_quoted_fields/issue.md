parse_line breaks on quoted fields

`parse_line` splits on every comma, so quoted fields are broken. `parse_line('a,"b,c",d')` should return `["a", "b,c", "d"]`. A doubled quote inside a quoted field means a literal quote: `parse_line('"say ""hi""",x')` should return `['say "hi"', "x"]`. Empty fields must be kept.
