use buggy_rust::{page_count, page_label};

#[test]
fn even_division() {
    assert_eq!(page_count(10, 5), 2);
}

#[test]
fn label() {
    assert_eq!(page_label(10, 5), "2");
}
