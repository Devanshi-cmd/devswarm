page_count drops the last partial page

`page_count(total, size)` returns too few pages when the total doesn't divide evenly. With 11 items and a page size of 5 there are 3 pages, but it returns 2. It should count a partial final page, and return 0 when there are no items.
