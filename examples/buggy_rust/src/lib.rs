/// How many pages are needed to show `total` items, `size` per page.
pub fn page_count(total: u32, size: u32) -> u32 {
    total / size
}

/// The page count as text.
pub fn page_label(total: u32, size: u32) -> String {
    itoa::Buffer::new().format(page_count(total, size)).to_string()
}
