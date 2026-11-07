# Crypto Portfolio Analysis data

Public exchange data that the Crypto Portfolio Analysis app reads. The files hold no
personal data.

| File | Content |
| --- | --- |
| `binance_spot_delist.json` | Coins whose Binance spot USDT market was closed |
| `binance_spot_swap.json` | Token swaps, rebrands and redenominations on Binance |
| `paribu_delist.json` | Coins that Paribu closed |
| `paribu_swap.json` | Token swaps, rebrands and redenominations on Paribu |

Each exchange has files of its own: one swap comes on another day on each exchange, or
not at all.

Each file is read as a whole. To correct a row, edit it; to withdraw one, delete it.
Times are UTC.

```json
{ "version": 1, "coins": { "ABC": "2026-01-31T03:00:00Z" } }
```

```json
{
  "version": 1,
  "swaps": [
    { "old": "ABC", "new": "XYZ", "ratio": 0.01, "rate": "100:1", "date": "2026-01-31T03:00:00Z" }
  ]
}
```

`rate` is written as old:new, so 100:1 means 100 old coins became 1 new coin. `ratio` is
how many new coins one old coin became. When `old` and `new` are the same, the coin keeps
its name and its amount changes.
