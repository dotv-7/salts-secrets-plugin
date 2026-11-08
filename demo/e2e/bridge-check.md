## What I check on a bridge

Four things, before anything else, and I now run the second one daily instead of weekly.

**One. Who can sign a release, how many of them, and who holds the keys.** Count the keys. Then read whether any are held outside the team. Five keys in one organisation is one key with extra steps; a single compromised laptop gets you to threshold. I prefer light-client verification. Failing that, external signers and a timelock on releases above a size. Basalt had neither.

**Two. Wrapped supply divided by locked collateral.** Pull both numbers, divide. It should be 1.000, every day. The user who posted at 04:22 did exactly this and beat the team by two hours. There is no analysis here. It is one division, and you can automate it.

**Three. Rate limits and pause.** Is there a cap on releases per hour? Basalt had none. A cap of 2,000 ETH an hour would have held the loss under 9,000 ETH even with the keys gone. Who can pause, and how fast have they ever actually done it? If the answer is "we have never tested it", the answer is four hours.

**Four. Where the pause button lives.** If the same keys that sign releases also sign the pause, a key compromise takes both. Basalt's pause was separate, which is the one thing it got right, and it was still too slow to matter.

Bridges are custodians that do not call themselves custodians. Treat them like one. Who holds it, who can move it, how do I know the claim matches the asset.
