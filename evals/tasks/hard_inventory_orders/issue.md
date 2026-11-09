Available stock counts drift from reality

Our warehouse numbers are wrong and we've seen these symptoms:

- Pressing cancel twice on an order puts its items back twice, so we show more stock than we have. Cancelling an order that is already cancelled should do nothing.
- When an order can't be placed because one item is out of stock, the other items in that order stay reserved. A failed order must leave stock exactly as it was, and should still raise OutOfStock.
- Cancelling an order that has already shipped puts its items back. That should raise OrderError and change nothing.
- Shipping a cancelled order should raise OrderError and leave it cancelled.
- Using an order id that doesn't exist should raise OrderError for both `ship` and `cancel`, not KeyError.
