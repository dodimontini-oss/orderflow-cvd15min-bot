# Operating status

Scheduled trading is paused. The corrected cached-data replay returned a 1.020 profit factor, 1.91% CAGR, and 21.93% maximum drawdown. A forced end-of-day exit reduced the result to a 1.011 profit factor and 0.62% CAGR. This is too close to break-even for unattended scheduling.

This strategy also trades QQQ in the same Alpaca paper account as both ORB bots and Orochi. Those processes cannot safely own and close separate QQQ positions in one netted account. Manual dispatch remains available for isolated testing.
