const test = require("node:test");
const assert = require("node:assert");
const { pageCount } = require("./pager.js");

test("even division", () => {
  assert.strictEqual(pageCount(10, 5), 2);
});
