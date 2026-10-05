# Performance Review

Load this method when a change affects hot paths, collection sizes, query counts, allocation, synchronization, caching, or I/O.

Look for a concrete workload and operation that became more expensive: repeated work inside a loop, an avoidable N+1 query, unbounded buffering, blocking I/O on an async path, unnecessary serialization/copying, lock contention, or a cache invalidation regression. Compare with the prior behavior and identify the scale at which impact appears.

Check whether batching, bounded input, an existing cache, or a caller-level optimization already addresses the concern. Do not report generic complexity, speculative future scale, or style preferences as performance defects. If impact depends on workload assumptions, state them and keep severity proportional.
