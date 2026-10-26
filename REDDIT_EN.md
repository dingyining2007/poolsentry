# I scanned 200 brand-new PancakeSwap V2 pairs. 83% have zero liquidity. Of the ones you *can* trade, 70% eat >20% of your $10 round trip.

**All numbers below are reproducible with a command I'm shipping. Full data JSON is in the repo.**

---

## Why I did this

Every "is this token safe" tool answers the same question: *is it a honeypot? is the tax 0?*

That's not the question I actually have when I'm about to ape into a new pair. My question is:

> **I put $10 in and immediately sell. How much comes back?**

You can pass every safety check, show "0% tax, can sell", and still hand me back $0.02 — because the pool is $0.03 deep. Those are two completely different questions, and the free tools only answer the first one.

So I wrote a tool that answers the second one, then pointed it at the newest 200 pairs to see what reality looked like.

---

## The data

PancakeSwap V2 factory (`0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73`), pairs `#3562025` through `#3562230` — i.e. the **last 200 pairs created**. Snapshot taken 2026-10-26.

Method: read `getReserves()`, `token0()`, `token1()`, `decimals()` straight from the pair contract over public BSC RPC. Closed-form `x*y=k` for a single hop, both directions, 0.25% fee per leg. No router, no aggregator, no third-party quote API.

### Result

| Bucket | Count | Share |
|---|---|---|
| **Created but zero liquidity** | 166 / 200 | **83.0%** |
| Has reserves, measurable | 34 / 200 | 17.0% |
| ├ $10 round trip loses **>20%** | 24 / 34 | **70.6%** |
| └ $10 round trip loses **>99.99%** | 14 / 34 | **41.2%** |

Distribution across the 34 tradeable pools:

```
  0%  – 1%     : 10   ██████████
  1%  – 5%     :  0
  5%  – 20%    :  0
  20% – 50%    :  2   ██
  50% – 99%    :  6   ██████
  99% – 100%   : 16   ████████████████
```

Notice the shape. **It's bimodal.** There is no middle. A pool is either fine (~0.5%, i.e. just the fee) or it eats you. Nothing lands in the 5–20% "meh" zone.

That matters: you can't eyeball this. A "looks alive, has a chart, has volume" pool is either 0.5% or 99%. There's no gradient to give you a warning sign.

---

## The most extreme case, worked by hand

`HAWK/WBNB` — pool `0x381d8e56bfba4a0ff78d1d48c43efdc5503e5e06`

```
token0 (HAWK) reserve : 487,685.354836   (18 decimals)
token1 (WBNB) reserve :       0.000047   (18 decimals)  ≈ $0.035
```

I put in $10:

- $10 → 0.01356 BNB
- buy: receive 485,991 HAWK
- sell immediately: get back **0.0000235 BNB = $0.0173**
- **loss: 99.83%**

This is not a bug in the formula. The pool holds 35 cents of WBNB. Ten dollars into a 35-cent pool destroys the price. That's the physics.

And in a wallet UI, this pair looks completely normal: you can buy, you can sell, tax is 0%, no honeypot flag. The only thing that's wrong is that the pool is empty.

---

## Why "can I sell?" is the wrong question

Free safety checkers test *whether the sell transaction reverts*. This pool's sell doesn't revert. It goes through perfectly. You just get $0.017 instead of $10.

The tool that tells you "not a honeypot ✅" is technically correct and completely useless here.

The two questions are:

1. **Can I sell?** ← what everyone checks
2. **How much do I get back when I sell?** ← what actually costs you money

This project only does #2, and it writes out the exact formula and the exact inputs so you can check my arithmetic instead of trusting my badge.

---

## What the tool does

`poolsentry check <pool_or_token_address>`

Reads the pool directly over RPC. Prints a round-trip table at four sizes:

```
① Round trip (buy then sell, closed-form single hop in this pool)
   $     10.00  →  back $      9.95   loss  0.50%
   $    213.68  →  back $    212.61   loss  0.50%
   $  2,136.77  →  back $  2,126.10   loss  0.50%
   $ 20,000.00  →  back $ 19,900.22   loss  0.50%
```

For a healthy pool (CAKE/WBNB, TVL $21.4M) all four rows sit at 0.50% — which is exactly the 0.25% × 2 fee floor. Nothing else is being taken from you.

For a broken pool, the curve bends hard as size grows, and you can see precisely where it breaks.

### It also deliberately does NOT do this

Earlier versions of this project had a bug: it quoted through the PancakeSwap **router** (`getAmountsOut`) instead of the pool itself. The router picks the best multi-hop route across all of BSC. For a thin new pair, that route might be "go through 3 hops and dump your token somewhere else."

I shipped a marketing claim based on that number — "the free tool says it's fine, but you'd actually lose 23.43%!" — and the real single-pool number was **0.4666%**. Off by **50×**. I was advertising my own bug as a feature.

The README now has that correction written into it permanently. The tool reads the pool, not the router, because when you're asking "how bad is *this pool*", the router's global best route is a lie.

---

## Reproduce it yourself

```bash
git clone https://github.com/dingyining2007/poolsentry
cd poolsentry

# re-run the 200-pair scan (~150 s)
N=200 python3 scripts/verify_68pct_fast.py

# check any single pool
python3 -m src.cli check 0x0ed7e52944161450477ee417de9cd3a859b14fd0
```

No API key. No account. No paid RPC. Public BSC endpoints only.

The 200-pool raw data (every pair address, every reserve value, every computed loss) is committed at `samples/newpairs_200_10usd_20261026_2114.json`. If you think my formula is wrong, the inputs are all there — recompute it and tell me.

---

## What I'm not claiming

- **This is not a safety audit.** A pool can be 0.50% round trip and still be a rug. Slippage cost and contract risk are orthogonal.
- **83% zero-liquidity is not 83% scams.** Most of those are pairs someone created and never seeded. Spam, noise, abandoned tests. It's not malice, it's the normal state of a permissionless factory.
- **n=200 is one snapshot, not a trend.** I'm not claiming this is true of all of BSC, or that it's getting worse, or better. It's 200 pairs at one block height on one day, and here's the script.
- **Fork/clone pools will fool it.** It detects PancakeSwap V2/V3 factory patterns. A lookalike factory with modified math will produce numbers I haven't validated.

---

## Where I'd actually want feedback

Three things I'm unsure about and would rather hear from people who've traded this market longer than I have:

1. **Is single-hop the right default, or should it be explicit?** When someone asks "what happens if I buy this token", and the router would route through 2 hops to a deeper pool — is the honest answer the single-hop number (this pool, as asked) or the router number (what you'd actually execute)? I picked single-hop because it answers the question asked, but I can see the argument both ways.

2. **Should zero-liquidity pairs be excluded from the statistic?** I reported 83% zero-liquidity as its own bucket rather than dropping it, because I think "created and abandoned" is itself information. But it does make the headline number softer.

3. **What's the loss threshold where you'd actually walk away?** The tool currently flags >2% as `HIGH_FEE_OR_TAX` (it used to be 5%, and I lowered it because "acceptable" was doing too much work in a label). For someone doing $200 trades on new pairs, is 2% the right line, or am I still being too generous?

---

Repo: https://github.com/dingyining2007/poolsentry

MIT licensed. Code is a few hundred lines of Python, no dependencies beyond `requests` and `eth_abi`. If the formula is wrong, open an issue with the pool address and the number you computed — I'd genuinely rather be corrected than be cited.
